import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("printbridge")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0+unknown"


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "printbridge.main:app",
        host=os.environ.get("PRINTBRIDGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("PRINTBRIDGE_PORT", "8000")),
    )
