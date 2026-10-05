import os
import threading
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from image_generator import ImageGenerator

load_dotenv()  # read keys from a local .env if present

app = FastAPI()

# Where generated images are written, and the in-memory record of each job's
# status. The dict is guarded by a lock because sync background tasks run in a
# threadpool, so the request thread and the task thread can touch it at once.
IMAGES_DIR = Path("images")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# Toggle for the optional profanity check (off unless ENABLE_PROFANITY_CHECK is set).
ENABLE_PROFANITY_CHECK = os.environ.get("ENABLE_PROFANITY_CHECK", "false").lower() in (
    "1", "true", "yes",
)


# Function to be run as a background task.
# This is just a placeholder function for demonstration.
# In your application, this could be a function that generates an image.
def write_log(message: str):
    # Example of a time-consuming task: Writing a message to a file.
    # Replace this with the logic of your image generation task.
    with open("log.txt", "a") as file:
        file.write(f"{message}\n")

@app.get("/example")
async def example_endpoint(background_tasks: BackgroundTasks):
    # This endpoint demonstrates how to add a background task.
    # The `write_log` function will be executed after the response is sent.
    # Note: The task runs in the same process but does not block the response.
    background_tasks.add_task(write_log, "Example endpoint was visited")
    return {"message": "This is an example endpoint"}


# --- lazy service builders (built on first use, never at import time) ------
# Building ImageGenerator reads the API key and opens a client, so we defer it.
# This also keeps importing app.py clean when no keys are set.

_generator: ImageGenerator | None = None


def get_generator() -> ImageGenerator:
    global _generator
    if _generator is None:
        api_key = os.environ.get("STABILITY_API_KEY")
        if not api_key:
            raise RuntimeError("STABILITY_API_KEY is not set. Copy _env to .env and fill it in.")
        _generator = ImageGenerator(api_key)
    return _generator


_profanity_service = None


def get_profanity_service():
    global _profanity_service
    if _profanity_service is None:
        from gpt_service import GPTService

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set. Copy _env to .env and fill it in.")
        prompt_path = Path("profanity_prompt.txt")
        system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        _profanity_service = GPTService(api_key, system_prompt)
    return _profanity_service


# --- custom prompt structure ----------------------------------------------
# A single validated free-text field. ImageGenerator already wraps it in a
# house style and a negative prompt, so this is the subject the user supplies.

class GenerateImageRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=1000)

    @field_validator("prompt")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least 3 non-whitespace characters")
        return value


# TODO (done): Background task function for image generation.
# Uses ImageGenerator to generate the image, saves it to disk, and updates the
# job's status. It never raises: a failure is recorded as status so a crash
# here cannot leave a job stuck on "processing".
def gen_image_task(image_id: str, prompt: str):
    try:
        image_bytes = get_generator().generate_image(prompt)
    except Exception as exc:  # bad key, network error, SDK error, ...
        with jobs_lock:
            jobs[image_id] = {"status": "failed", "detail": f"generation error: {exc}"}
        return

    if not image_bytes:
        # The generator returns None when Stability's safety filter trips or no
        # image artifact comes back.
        with jobs_lock:
            jobs[image_id] = {
                "status": "failed",
                "detail": "no image returned (prompt may have been filtered)",
            }
        return

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / f"{image_id}.png").write_bytes(image_bytes)
    with jobs_lock:
        jobs[image_id] = {"status": "ready", "detail": None}


# TODO (done): POST /images endpoint for asynchronous image generation.
# Validates the prompt, optionally runs the profanity check, registers the job,
# schedules generation in the background, and returns an id to poll with.
@app.post("/images", status_code=202)
async def create_image(request: GenerateImageRequest, background_tasks: BackgroundTasks):
    prompt = request.prompt

    # OPTIONAL: profanity / validation check before doing any work.
    # contains_profanity makes a blocking network call, so run it in a threadpool.
    if ENABLE_PROFANITY_CHECK:
        flagged = await run_in_threadpool(get_profanity_service().contains_profanity, prompt)
        if flagged:
            raise HTTPException(status_code=400, detail="Prompt rejected by the content filter.")

    image_id = uuid4().hex
    with jobs_lock:
        jobs[image_id] = {"status": "processing", "detail": None}

    background_tasks.add_task(gen_image_task, image_id, prompt)
    return {"image_id": image_id, "status": "processing"}


# TODO (done): GET /image/{image_id} to retrieve the image or its status.
# 404 unknown id, 202 still processing, 200 the PNG when ready, 500 on failure.
@app.get("/image/{image_id}")
async def get_image(image_id: str):
    with jobs_lock:
        job = jobs.get(image_id)
        job = dict(job) if job is not None else None

    if job is None:
        raise HTTPException(status_code=404, detail="image_id not found")

    if job["status"] == "ready":
        path = IMAGES_DIR / f"{image_id}.png"
        if not path.exists():
            raise HTTPException(status_code=500, detail="image marked ready but file is missing")
        return FileResponse(path, media_type="image/png", filename=f"{image_id}.png")

    if job["status"] == "failed":
        raise HTTPException(status_code=500, detail=job["detail"] or "image generation failed")

    # still processing
    return JSONResponse(status_code=202, content={"image_id": image_id, **job})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
