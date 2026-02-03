import os
import time
import base64
import mimetypes
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --- INICIALIZAÇÃO ---
app = FastAPI(title="DEPTS 3D - Gerador STL", version="1.1.0")

# --- CONFIGURAÇÃO DE SEGURANÇA (CORS) ---
# Essencial para que o site da Hostinger consiga falar com o Render
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configurações da API Meshy via Variáveis de Ambiente no Render
MESHY_API_KEY = os.getenv("MESHY_API_KEY")
MESHY_BASE_URL = "https://api.meshy.ai"

class STLResponse(BaseModel):
    image_to_3d_task_id: str
    remesh_task_id: str
    stl_url: str

# --- FUNÇÕES AUXILIARES ---

def _to_data_uri(file_bytes: bytes, filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    if not mime: mime = "image/png"
    b64 = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"

async def _meshy_request(client: httpx.AsyncClient, method: str, path: str, json: Optional[Dict[str, Any]] = None):
    if not MESHY_API_KEY:
        print("ERRO: MESHY_API_KEY não encontrada nas variáveis de ambiente.")
        raise HTTPException(status_code=500, detail="Configuração de API Key ausente.")
    
    url = f"{MESHY_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {MESHY_API_KEY}"}
    
    print(f"Enviando requisição para Meshy: {method} {path}")
    r = await client.request(method, url, headers=headers, json=json)
    
    if r.status_code >= 400:
        print(f"Erro na API Meshy ({r.status_code}): {r.text}")
        raise HTTPException(status_code=502, detail=f"Erro na Meshy: {r.text}")
    
    return r.json()

async def _wait_task(client: httpx.AsyncClient, path: str, poll_seconds: int = 5):
    print(f"Iniciando monitoramento da tarefa: {path}")
    for i in range(120):  # Monitora por até 10 minutos
        data = await _meshy_request(client, "GET", path)
        status = data.get("status")
        progress = data.get("progress")
        
        print(f"Status da tarefa: {status} ({progress}%)")
        
        if status == "SUCCEEDED":
            return data
        if status in ("FAILED", "CANCELED"):
            print(f"Tarefa falhou ou foi cancelada. Dados: {data}")
            return None
        
        time.sleep(poll_seconds)
    return None

# --- ROTAS DA API ---

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/stl", response_model=STLResponse)
async def generate_stl(image: UploadFile = File(...)):
    print(f"Recebida nova imagem para processar: {image.filename}")
    
    file_bytes = await image.read()
    image_uri = _to_data_uri(file_bytes, image.filename)

    # Aumentado o timeout para 300 segundos (5 minutos) para evitar erro 502 Gateway
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
        try:
            # 1. Cria a tarefa Image-to-3D
            print("Passo 1: Criando tarefa Image-to-3D...")
            res = await _meshy_request(client, "POST", "/openapi/v1/image-to-3d", 
                json={"image_url": image_uri, "ai_model": "meshy-5", "should_remesh": True})
            task_id = res.get("result")
            
            # 2. Aguarda processamento da IA
            print(f"Passo 2: Aguardando IA processar a Task ID: {task_id}")
            task_result = await _wait_task(client, f"/openapi/v1/image-to-3d/{task_id}")
            if not task_result:
                raise HTTPException(status_code=500, detail="IA falhou ao processar imagem.")

            # 3. Solicita conversão específica para formato STL (Remesh)
            print("Passo 3: Solicitando conversão para STL (Remesh)...")
            remesh = await _meshy_request(client, "POST", "/openapi/v1/remesh",
                json={"input_task_id": task_id, "target_formats": ["stl"], "convert_format_only": True})
            remesh_id = remesh.get("result")

            # 4. Aguarda link final do arquivo STL
            print(f"Passo 4: Aguardando link final do arquivo (Remesh ID: {remesh_id})")
            final_data = await _wait_task(client, f"/openapi/v1/remesh/{remesh_id}")
            
            stl_url = final_data.get("model_urls", {}).get("stl") if final_data else None

            if not stl_url:
                print("ERRO: Link do STL não foi retornado pela Meshy.")
                raise HTTPException(status_code=500, detail="Erro ao obter link final do arquivo STL.")

            print(f"Sucesso! STL gerado: {stl_url}")
            return STLResponse(
                image_to_3d_task_id=task_id, 
                remesh_task_id=remesh_id, 
                stl_url=stl_url
            )

        except Exception as e:
            print(f"ERRO CRÍTICO NO BACKEND: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
