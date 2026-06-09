"""
FastAPI application.

Runs identically in two environments:
  - Locally:    uvicorn api.index:app --reload   (from the repo root)
  - On Vercel:  vercel.json rewrites every path to this ASGI app.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

try:  # Vercel imports this file as a top-level module; locally it's a package.
    from api.verifier import ApplicationData, VerificationResult, verify_label
except ImportError:  # pragma: no cover
    from verifier import ApplicationData, VerificationResult, verify_label

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # stay under Vercel's 4.5 MB body limit

app = FastAPI(title="TTB Label Verification Prototype")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/verify", response_model=VerificationResult)
async def verify(
    image: UploadFile = File(...),
    brand_name: str = Form(...),
    class_type: str = Form(...),
    alcohol_content: str = Form(...),
    net_contents: str = Form(...),
) -> VerificationResult:
    if image.content_type not in ALLOWED_TYPES:
        raise HTTPException(415, "Please upload a JPEG, PNG, or WebP image.")

    data = await image.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, "Image is larger than 4 MB. Please use a smaller image."
        )
    if not data:
        raise HTTPException(400, "The uploaded image is empty.")

    application = ApplicationData(
        brand_name=brand_name.strip(),
        class_type=class_type.strip(),
        alcohol_content=alcohol_content.strip(),
        net_contents=net_contents.strip(),
    )
    for field_name, value in application.model_dump().items():
        if not value:
            raise HTTPException(422, f"Application field '{field_name}' is required.")

    try:
        return verify_label(data, image.content_type, application)
    except RuntimeError as exc:  # missing API key, etc.
        logger.exception("RuntimeError in verify_label")
        raise HTTPException(500, str(exc))
    except Exception:
        logger.exception("Unexpected error in verify_label")
        raise HTTPException(
            502,
            "The verification service could not process this image. Please try again.",
        )


@app.exception_handler(HTTPException)
async def http_exc_handler(_, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})