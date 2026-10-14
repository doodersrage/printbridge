import os


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "printbridge.main:app",
        host=os.environ.get("PRINTBRIDGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("PRINTBRIDGE_PORT", "8000")),
    )
