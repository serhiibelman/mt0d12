from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/")
def status_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "status.html", media_type="text/html")
