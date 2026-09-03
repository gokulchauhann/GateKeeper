from fastapi import FastAPI


app = FastAPI(title="Gatekeeper")


@app.get("/health")
def health_check():
    return {"status": "ok"}