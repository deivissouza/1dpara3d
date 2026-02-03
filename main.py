@app.post("/stl", response_model=STLResponse)
async def generate_stl(image: UploadFile = File(...)):
    file_bytes = await image.read()
    image_data_uri = _to_data_uri(file_bytes, image.filename)

    # Aumentamos o timeout para 300 segundos (5 minutos)
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
        try:
            # 1) Gerar 3D
            create_i2t3d = await _meshy_request(client, "POST", "/openapi/v1/image-to-3d", 
                json={"image_url": image_data_uri, "ai_model": "meshy-5", "should_remesh": True})
            i2t3d_id = create_i2t3d.get("result")
            print(f"Tarefa criada: {i2t3d_id}")

            # 2) Aguardar (Polling)
            await _wait_task(client, f"/openapi/v1/image-to-3d/{i2t3d_id}", 5, 600)

            # 3) Converter para STL
            remesh_create = await _meshy_request(client, "POST", "/openapi/v1/remesh",
                json={"input_task_id": i2t3d_id, "target_formats": ["stl"], "convert_format_only": True})
            remesh_id = remesh_create.get("result")

            # 4) Pegar link final
            remesh_task = await _wait_task(client, f"/openapi/v1/remesh/{remesh_id}", 5, 300)
            stl_url = remesh_task.get("model_urls", {}).get("stl")

            return STLResponse(image_to_3d_task_id=i2t3d_id, remesh_task_id=remesh_id, stl_url=stl_url)
        
        except Exception as e:
            print(f"ERRO DETALHADO: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))
