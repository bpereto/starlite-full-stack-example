from __future__ import annotations

import atexit
import contextlib
import json
import threading
from dataclasses import dataclass, field
from functools import cached_property
from inspect import isclass
from multiprocessing.util import _exit_function  # type: ignore[attr-defined]
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Generic, TypeVar, cast
from urllib.parse import urljoin

import anyio
import markupsafe
from anyio import open_process
from anyio.streams.text import TextReceiveStream
from pydantic import BaseModel
from starlite.contrib.jinja import JinjaTemplateEngine
from starlite.exceptions import ImproperlyConfiguredException
from starlite.template import TemplateEngineProtocol

from app.lib import log, settings

__all__ = ["ViteAssetLoader", "ViteConfig", "ViteTemplateConfig", "ViteTemplateEngine", "run_vite"]


if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import DirectoryPath
    from starlite.types import PathType

T = TypeVar("T", bound=TemplateEngineProtocol)

# TODO: Maybe re-write all of this?  This is from my old project `fastapi-vite` that was based an on even older project.  It's been slightly refactored so that it works in starlite.
# when running the app with "DEBUG" == true : Adds tags to serve the vite JS websocket for hot module reloading with the vite dev server is running
# when running the app with "DEBUG" == false (default)  : Adds tags that parse the manifest.json file generated by the `npm run build` command

# generic template is provided at `/templates/site/index.html`
MANIFEST_NAME: Final = "manifest.json"


class ViteConfig(BaseModel):
    """Configuration for ViteJS support.

    To enable Vite integration, pass an instance of this class to the
    :class:`Starlite <starlite.app.Starlite>` constructor using the
    'plugins' key.
    """

    hot_reload: bool = False
    is_react: bool = False
    static_url: str = "/static/"
    """Base URL to generate for static asset references.

    This should match what you have for the STATIC_URL
    """
    static_dir: Path
    """Location of the manifest file.

    The path relative to the `assets_path` location
    """
    host: str = "localhost"
    protocol: str = "http"
    port: int = 3000
    run_command: list[str] = ["npm", "run", "dev"]
    build_command: list = ["npm", "run", "build"]


vite_config = ViteConfig.parse_obj(
    {"hot_reload": settings.app.DEBUG, "assets_path": settings.app.STATIC_URL, "static_dir": settings.app.STATIC_DIR},
)


class ViteAssetLoader:
    """Vite  manifest loader.

    Please see: https://vitejs.dev/guide/backend-integration.html
    """

    _instance: ClassVar[ViteAssetLoader | None] = None
    _manifest: ClassVar[dict[str, Any]] = {}

    def __new__(cls) -> ViteAssetLoader:
        """Singleton manifest loader."""
        if cls._instance is None:
            cls._manifest = cls.parse_manifest()
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def parse_manifest() -> dict[str, Any]:
        """Read and parse the Vite manifest file.

        Example manifest:
        ```json
            {
                "main.js": {
                    "file": "assets/main.4889e940.js",
                    "src": "main.js",
                    "isEntry": true,
                    "dynamicImports": ["views/foo.js"],
                    "css": ["assets/main.b82dbe22.css"],
                    "assets": ["assets/asset.0ab0f9cd.png"]
                },
                "views/foo.js": {
                    "file": "assets/foo.869aea0d.js",
                    "src": "views/foo.js",
                    "isDynamicEntry": true,
                    "imports": ["_shared.83069a53.js"]
                },
                "_shared.83069a53.js": {
                    "file": "assets/shared.83069a53.js"
                }
                }
        ```

        Raises:
            RuntimeError: if cannot load the file or JSON in file is malformed.
        """
        manifest = {}
        if not vite_config.hot_reload:
            with Path(vite_config.static_dir / MANIFEST_NAME).open() as manifest_file:
                manifest_content = manifest_file.read()
            try:
                manifest = json.loads(manifest_content)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "Cannot read Vite manifest file at %s",
                    Path(vite_config.static_dir / MANIFEST_NAME),
                ) from exc
        return manifest

    def generate_vite_ws_client(self) -> str:
        """Generate the script tag for the Vite WS client for HMR.

        Only used when hot module reloading is enabled, in production this method returns an empty string.

        Returns:
            str: The script tag or an empty string.
        """
        if not vite_config.hot_reload:
            return ""

        return self._script_tag(
            self._vite_server_url("@vite/client"),
            {"type": "module"},
        )

    def generate_vite_react_hmr(self) -> str:
        """Generate the script tag for the Vite WS client for HMR.

        Only used when hot module reloading is enabled, in production this method returns an empty string.

        Returns:
            str: The script tag or an empty string.
        """
        if vite_config.is_react and vite_config.hot_reload:
            return f"""
                <script type="module">
                import RefreshRuntime from '{self._vite_server_url()}@react-refresh'
                RefreshRuntime.injectIntoGlobalHook(window)
                window.$RefreshReg$ = () => {{}}
                window.$RefreshSig$ = () => (type) => type
                window.__vite_plugin_react_preamble_installed__=true
                </script>
                """
        return ""

    def generate_vite_asset(self, path: str, scripts_attrs: dict[str, str] | None = None) -> str:
        """Generate all assets include tags for the file in argument.

        Returns:
            str: All tags to import this asset in your HTML page.
        """
        if vite_config.hot_reload:
            return self._script_tag(
                self._vite_server_url(path),
                {"type": "module", "async": "", "defer": ""},
            )

        if path not in self._manifest:
            raise RuntimeError(
                "Cannot find %s in Vite manifest at %s",
                path,
                Path(vite_config.static_dir / MANIFEST_NAME),
            )

        tags = []
        manifest_entry: dict = self._manifest[path]
        if not scripts_attrs:
            scripts_attrs = {"type": "module", "async": "", "defer": ""}

        # Add dependent CSS
        if "css" in manifest_entry:
            for css_path in manifest_entry.get("css", {}):
                tags.append(self._style_tag(urljoin(vite_config.static_url, css_path)))

        # Add dependent "vendor"
        if "imports" in manifest_entry:
            for vendor_path in manifest_entry.get("imports", {}):
                tags.append(self.generate_vite_asset(vendor_path, scripts_attrs=scripts_attrs))

        # Add the script by itself
        tags.append(
            self._script_tag(
                urljoin(vite_config.static_url, manifest_entry["file"]),
                attrs=scripts_attrs,
            ),
        )

        return "\n".join(tags)

    def _vite_server_url(self, path: str | None = None) -> str:
        """Generate an URL to and asset served by the Vite development server.

        Keyword Arguments:
            path {Optional[str]}: Path to the asset. (default: {None})

        Returns:
            str: Full URL to the asset.
        """
        base_path = "{protocol}://{host}:{port}".format(
            protocol=vite_config.protocol,
            host=vite_config.host,
            port=vite_config.port,
        )
        return urljoin(
            base_path,
            urljoin(vite_config.static_url, path if path is not None else ""),
        )

    def _script_tag(self, src: str, attrs: dict[str, str] | None = None) -> str:
        """Generate an HTML script tag."""
        attrs_str = ""
        if attrs is not None:
            attrs_str = " ".join([f'{key}="{value}"' for key, value in attrs.items()])

        return f'<script {attrs_str} src="{src}"></script>'

    def _style_tag(self, href: str) -> str:
        """Generate and HTML <link> stylesheet tag for CSS.

        Args:
            href: CSS file URL.

        Returns:
            str: CSS link tag.
        """
        return f'<link rel="stylesheet" href="{href}" />'


class ViteTemplateEngine(JinjaTemplateEngine):
    """Jinja Template Engine with Vite Integration."""

    def __init__(self, directory: DirectoryPath | list[DirectoryPath]) -> None:
        """Implement Vite templates with the default JinjaTemplateEngine."""
        super().__init__(directory)
        self._vite_asset_loader = ViteAssetLoader()
        self.engine.globals["vite_hmr_client"] = self.hmr_client
        self.engine.globals["vite_asset"] = self.resource

    def hmr_client(self) -> markupsafe.Markup:
        """Generate the script tag for the Vite WS client for HMR.

        Only used when hot module reloading is enabled, in production this method returns an empty string.


        Returns:
            str: The script tag or an empty string.
        """
        tags: list = []
        tags.append(self._vite_asset_loader.generate_vite_react_hmr())
        tags.append(self._vite_asset_loader.generate_vite_ws_client())
        return markupsafe.Markup("\n".join(tags))

    def resource(self, path: str, scripts_attrs: dict[str, str] | None = None) -> markupsafe.Markup:
        """Generate all assets include tags for the file in argument.

        Generates all scripts tags for this file and all its dependencies
        (JS and CSS) by reading the manifest file (for production only).
        In development Vite imports all dependencies by itself.
        Place this tag in <head> section of your page
        (this function marks automatically <script> as "async" and "defer").

        Arguments:
            path: Path to a Vite asset to include.
            scripts_attrs: script attributes

        Keyword Arguments:
            scripts_attrs {Optional[Dict[str, str]]}: Override attributes added to scripts tags. (default: {None})

        Returns:
            str: All tags to import this asset in your HTML page.
        """
        return markupsafe.Markup(self._vite_asset_loader.generate_vite_asset(path, scripts_attrs=scripts_attrs))


@dataclass
class ViteTemplateConfig(Generic[T]):
    """Configuration for Templating.

    To enable templating, pass an instance of this class to the
    :class:`Starlite <starlite.app.Starlite>` constructor using the
    'template_config' key.
    """

    engine: type[ViteTemplateEngine] | ViteTemplateEngine
    """A template engine adhering to the :class:`TemplateEngineProtocol
    <starlite.template.base.TemplateEngineProtocol>`."""
    config: ViteConfig
    """A a config for the vite engine`."""
    directory: PathType | list[PathType] | None = field(default=None)
    """A directory or list of directories from which to serve templates."""
    engine_callback: Callable[[T], None] | None = field(default=None)
    """A callback function that allows modifying the instantiated templating
    protocol."""

    def __post_init__(self) -> None:
        """Ensure that directory is set if engine is a class."""
        if isclass(self.engine) and not self.directory:
            raise ImproperlyConfiguredException("directory is a required kwarg when passing a template engine class")

    def to_engine(self) -> T:
        """Instantiate the template engine."""
        template_engine = cast("T", self.engine(self.directory) if isclass(self.engine) else self.engine)
        if callable(self.engine_callback):
            self.engine_callback(template_engine)
        return template_engine

    @cached_property
    def engine_instance(self) -> T:
        """Return the template engine instance."""
        return self.to_engine()


template_config: ViteTemplateConfig = ViteTemplateConfig(
    directory=settings.TEMPLATES_DIR,
    engine=ViteTemplateEngine,
    config=vite_config,
)


if threading.current_thread() is not threading.main_thread():
    atexit.unregister(_exit_function)


logger = log.get_logger()


def run_vite() -> None:
    """Run Vite in a subprocess.

    Args:
        vite_config (ViteConfig): _description_
    """
    log.config.configure()
    with contextlib.suppress(KeyboardInterrupt):
        try:
            anyio.run(_run_vite, backend="asyncio", backend_options={"use_uvloop": True})
        finally:
            logger.info("Vite Service stopped.")


async def _run_vite() -> None:
    """Run Vite in a subprocess.

    Args:
        vite_config (ViteConfig): _description_
    """
    async with await open_process(template_config.config.run_command) as vite_process:
        async for text in TextReceiveStream(vite_process.stdout):  # type: ignore[arg-type]
            await logger.ainfo("Vite", message=text.replace("\n", ""))
