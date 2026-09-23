import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from cache import tree_cache

logging.basicConfig(level=logging.INFO)

# How often to rebuild in the background. Tune to how often the
# underlying data actually changes - or set very high and rely on
# POST /refresh triggered from wherever writes happen.
REFRESH_INTERVAL_SECONDS = 300


async def periodic_refresh():
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(tree_cache.rebuild)
        except Exception:
            logging.exception("Background tree refresh failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build once at startup so the first request is already fast.
    await asyncio.to_thread(tree_cache.rebuild)
    task = asyncio.create_task(periodic_refresh())
    yield
    task.cancel()


app = FastAPI(title="rmd_aclist tree API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this for production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/tree")
async def get_tree():
    """Full tree. Served as pre-serialized bytes - no build or
    validation cost per request."""
    return Response(content=tree_cache.get_full_tree_bytes(), media_type="application/json")


@app.get("/tree/{acid}")
async def get_subtree(acid: str):
    """Just the subtree rooted at acid, including all descendants."""
    data = tree_cache.get_subtree_bytes(acid)
    if data is None:
        raise HTTPException(404, f"acid {acid} not found")
    return Response(content=data, media_type="application/json")


@app.get("/children/{acid}")
async def get_children(acid: str):
    """One level of children only - for expandable/lazy-loading UI
    trees that don't want the whole payload at once."""
    data = tree_cache.get_children_bytes(acid)
    if data is None:
        raise HTTPException(404, f"acid {acid} not found")
    return Response(content=data, media_type="application/json")


@app.post("/refresh")
async def refresh():
    """Force an immediate rebuild - call this from a trigger/webhook
    right after writes to rmd_aclist if you need fresher-than-5-minute data."""
    elapsed = await asyncio.to_thread(tree_cache.rebuild)
    return {"status": "ok", "seconds": round(elapsed, 3), **tree_cache.stats()}


@app.get("/health")
async def health():
    return {"status": "ok", **tree_cache.stats()}
