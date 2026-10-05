# Stable Diffusion Image Generation API

A FastAPI service that turns a text prompt into an image using Stability AI's
Stable Diffusion. Generation is slow, so it never runs inside a request: the API
accepts a prompt, starts the work in the background, and returns an id the caller
polls for the result.

Everything lives in a single `app.py`. The only extra pieces are the provided
`image_generator/` package (the Stability wrapper) and, if the optional profanity
check is turned on, `gpt_service.py` plus `profanity_prompt.txt`.

## How it works

```
POST /images  ->  validate prompt  ->  record job (processing)  ->  BackgroundTasks  ->  return image_id
                                                                          |
                                      gen_image_task: call Stability, save PNG, set ready / failed

GET /image/{image_id}  ->  404 unknown | 202 processing | 200 PNG | 500 failed
```

Job status is kept in an in-memory dict and the generated PNG is written to an
`images/` folder. The slow Stability call runs in a background task, so the
endpoint returns immediately and the server stays responsive.

## Setup

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```sh
cd assignment
uv sync
copy _env .env        # Windows (PowerShell). On macOS/Linux: cp _env .env
```

`.env` holds the keys:

```
STABILITY_API_KEY=sk-...     # required to generate images
OPENAI_API_KEY=sk-...        # only needed if the profanity check is enabled
```

Optional setting:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_PROFANITY_CHECK` | `false` | `true` rejects flagged prompts with `400` before generating |

> The `.env` must sit in the `assignment/` folder, because that is where you run
> the server from and where it looks for the file.

## Running

```sh
uv run uvicorn app:app --reload
```

The server starts on http://127.0.0.1:8000. It does not open a browser by
itself; go to the interactive docs at http://127.0.0.1:8000/docs. The root URL
`/` is not defined, so it returns `{"detail":"Not Found"}`, which is expected.

## API

### `POST /images`

Body:

```json
{ "prompt": "a lighthouse on a rocky coast at dawn" }
```

The prompt is validated (3 to 1000 non-blank characters) before any work, so bad
input is rejected with `422` and never reaches the paid API. Returns `202`:

```json
{ "image_id": "ec26f789...", "status": "processing" }
```

```sh
curl -X POST http://127.0.0.1:8000/images \
  -H "content-type: application/json" \
  -d "{\"prompt\": \"a lighthouse on a rocky coast at dawn\"}"
```

### `GET /image/{image_id}`

| State | Code | Body |
| --- | --- | --- |
| Unknown id | `404` | `{"detail": "image_id not found"}` |
| Still working | `202` | `{"image_id", "status": "processing", "detail": null}` |
| Ready | `200` | the PNG (`image/png`) |
| Failed | `500` | `{"detail": "<reason>"}` |

```sh
curl -o out.png http://127.0.0.1:8000/image/ec26f789...
```

### `GET /example`

Unchanged starter endpoint, kept so the existing test suite passes.

## The custom prompt

Input is a single validated `prompt` string. The provided `ImageGenerator`
already wraps it in a house style and applies a long negative prompt, so the
user's text fills the subject slot of
`"{user_input} comical sketch on paper, highly detailed, high resolution"`. This
matches the generator's one-image-per-call behaviour (`samples=1`): one prompt
in, one `image_id` out, one image to fetch.

## Design decisions

* **Slow work off the request.** The Stability call takes seconds and blocks, so
  it runs in a `BackgroundTasks` task, never in the request. The endpoint returns
  an id straight away.
* **Sync task function.** `gen_image_task` is a plain `def`. FastAPI runs sync
  background tasks in a threadpool, which is what a blocking call needs so it does
  not stall the event loop.
* **Status in memory, bytes on disk.** Status is small and kept in a `jobs` dict
  guarded by a lock (the threadpool task and the request thread can touch it at
  once). Images are large and binary, so they are written to `images/`.
* **Lazy initialization.** The `ImageGenerator` (and the profanity service) are
  built on first use, not at import, so importing `app.py` with no keys set stays
  clean, which the test suite relies on.
* **Failures become status, not crashes.** `gen_image_task` catches everything and
  records `failed` with a reason, including the `None` the generator returns when
  Stability's safety filter trips. A job never gets stuck on `processing`.
* **Profanity check is pre-flight and optional.** When enabled it runs before the
  job starts (so a bad prompt is rejected synchronously with `400`) and is pushed
  to a threadpool because it makes a blocking network call.

## Notes

* Generation requires a funded Stability account. With no credits the SDK returns
  `RESOURCE_EXHAUSTED`, which this app surfaces as a `500` with the reason in
  `detail`. That is the error handling working as intended, not an app bug.
* The Redis queue (listed as optional in the brief) is not included in this
  single-file version; it uses FastAPI `BackgroundTasks` only.

## Tests

From the `assignment` directory:

```sh
uv run python -m unittest discover -s ../tests -v
```
