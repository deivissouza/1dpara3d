import os
import time
import base64
import mimetypes
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- CONFIGURAÇÃO DE SEGURANÇA (CORS) ---
# Isso permite que o seu site na Hostinger acesse este script no Render
app = FastAPI(title="Meshy STL Generator", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção, você pode trocar "*" pelo seu domínio depts3d.com
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MESHY_API_KEY = os.getenv("MESHY_API_KEY")
MESHY_BASE_URL = "https://api.meshy.ai"

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 12 * 60  

class STLResponse(BaseModel):
    image_to_3d_task_id: str
    remesh_task_id: str
    stl_url: str

def _guess_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    if mime in ("image/png", "image/jpeg"):
        return mime
    return "image/png"

def _to_data_uri(file_bytes: bytes, filename: str) -> str:
    mime = _guess_mime(filename)
    b64 = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"

async def _meshy_request(client: httpx.AsyncClient, method: str, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not MESHY_API_KEY:
        raise HTTPException(status_code=500, detail="MESHY_API_KEY não configurada.")
    url = f"{MESHY_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {MESHY_API_KEY}", "Content-Type": "application/json"}
    r = await client.request(method, url, headers=headers, json=json)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Erro Meshy: {r.text}")
    return r.json()

async def _wait_task(client: httpx.AsyncClient, get_path: str, poll_seconds: float, timeout_seconds: int) -> Dict[str, Any]:
    start = time.time()
    while True:
        data = await _meshy_request(client, "GET", get_path)
        status = data.get("status")
        if status == "SUCCEEDED": return data
        if status in ("FAILED", "CANCELED"):
            raise HTTPException(status_code=502, detail="Tarefa falhou na Meshy.")
        if (time.time() - start) > timeout_seconds:
            raise HTTPException(status_code=504, detail="Timeout.")
        time.sleep(poll_seconds)

@app.get("/health")
def health(): return {"ok": True}

@app.post("/stl", response_model=STLResponse)
async def generate_stl(image: UploadFile = File(...)):
    file_bytes = await image.read()
    image_data_uri = _to_data_uri(file_bytes, image.filename)

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # 1) Gerar 3D
        create_i2t3d = await _meshy_request(client, "POST", "/openapi/v1/image-to-3d", 
            json={"image_url": image_data_uri, "ai_model": "meshy-5", "should_remesh": True})
        i2t3d_id = create_i2t3d.get("result")

        # 2) Aguardar
        await _wait_task(client, f"/openapi/v1/image-to-3d/{i2t3d_id}", 5, 600)

        # 3) Converter para STL
        remesh_create = await _meshy_request(client, "POST", "/openapi/v1/remesh",
            json={"input_task_id": i2t3d_id, "target_formats": ["stl"], "convert_format_only": True})
        remesh_id = remesh_create.get("result")

        # 4) Aguardar final e pegar link
        remesh_task = await _wait_task(client, f"/openapi/v1/remesh/{remesh_id}", 5, 300)
        stl_url = remesh_task.get("model_urls", {}).get("stl")

        return STLResponse(image_to_3d_task_id=i2t3d_id, remesh_task_id=remesh_id, stl_url=stl_url)