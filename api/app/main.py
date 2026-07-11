from app.routes import auth, experiments, prompts
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Prompt Versioning & A/B Testing Platform",
    description="Git for prompts — traffic splitting, significance testing, automatic winner declaration.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(prompts.router)
app.include_router(experiments.router)


@app.get("/health")
def health():
    return {"status": "ok"}
