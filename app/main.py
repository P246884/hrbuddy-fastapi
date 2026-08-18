from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routes.chat import router as chat_router

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

# Routes
app.include_router(chat_router)


@app.on_event("startup")
def _warm_policy_index():
    """Build the policy search index once, in the background, as soon as the
    server starts — so the FIRST user question is fast instead of waiting for
    a cold index build. Runs in a daemon thread so it never blocks startup."""
    import threading

    def _build():
        try:
            from app.rag import policy_rag
            policy_rag.build_index()
            print("POLICY INDEX: warmed on startup")
            # warm the Ollama models too, so the FIRST real question doesn't
            # pay the model-load cost (embed + chat cold start).
            try:
                policy_rag._embed("warmup")
                if policy_rag._ollama_client:
                    policy_rag._ollama_client.chat(
                        model=policy_rag.CHAT_MODEL,
                        messages=[{"role": "user", "content": "hi"}],
                        options={"num_predict": 1},
                    )
                print("POLICY MODELS: warmed")
            except Exception as ex2:
                print("model warm skipped:", ex2)
        except Exception as ex:
            print("POLICY INDEX warm skipped:", ex)

    threading.Thread(target=_build, daemon=True).start()


from fastapi.responses import FileResponse


@app.get("/policy/download")
def download_policy(file: str):
    """Serve a policy PDF for download by filename. The chat stream sends only
    the filename (small), and the download button hits this endpoint — so the
    huge base64 PDF never travels through the answer stream."""
    from app.rag import policy_rag
    path = policy_rag.policy_path(file)
    if not path:
        raise HTTPException(status_code=404, detail="Policy not found")
    return FileResponse(path, media_type="application/pdf",
                        filename=file)


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )