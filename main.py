import os
import random
import string
import aiosqlite
import aiofiles
from datetime import datetime
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "uploads"
DB_PATH = "files.db"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB

os.makedirs(UPLOAD_DIR, exist_ok=True)

async def get_db():
    """获取异步数据库连接"""
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                code TEXT PRIMARY KEY,
                filename TEXT,
                size INTEGER,
                upload_time TEXT
            )
        """)
        await db.commit()

@app.on_event("startup")
async def startup():
    await init_db()

async def generate_unique_code(db: aiosqlite.Connection) -> str:
    """在给定数据库连接中生成唯一码（调用前需确保持有连接）"""
    characters = string.ascii_letters + string.digits
    for _ in range(100):
        code = ''.join(random.choices(characters, k=5))
        cursor = await db.execute("SELECT 1 FROM files WHERE code = ?", (code,))
        row = await cursor.fetchone()
        if not row:
            return code
    raise HTTPException(status_code=500, detail="无法生成唯一文件码，请稍后重试")

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    original_filename = file.filename or "unnamed"
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 使用一个数据库连接处理插入占位和最后更新，避免连接开销
    db = await get_db()
    try:
        code = await generate_unique_code(db)
        await db.execute(
            "INSERT INTO files (code, filename, size, upload_time) VALUES (?, ?, 0, ?)",
            (code, original_filename, upload_time)
        )
        await db.commit()
    except Exception:
        await db.close()
        raise HTTPException(status_code=500, detail="文件码生成失败")
    # 注意：此时暂不关闭数据库，下面更新时还要用

    _, ext = os.path.splitext(original_filename)
    ext = ext.lstrip(".")[:10]
    safe_filename = f"{code}.{ext}" if ext else code
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    file_size = 0
    try:
        async with aiofiles.open(file_path, "wb") as out_file:
            while chunk := await file.read(1024 * 1024):  # 异步读取上传流
                file_size += len(chunk)
                if file_size > MAX_FILE_SIZE:
                    await out_file.close()
                    os.remove(file_path)
                    # 回滚数据库记录
                    await db.execute("DELETE FROM files WHERE code = ?", (code,))
                    await db.commit()
                    await db.close()
                    raise HTTPException(status_code=413, detail="文件大小超过500MB限制")
                await out_file.write(chunk)
    except Exception:
        if os.path.exists(file_path):
            os.remove(file_path)
        await db.execute("DELETE FROM files WHERE code = ?", (code,))
        await db.commit()
        await db.close()
        raise HTTPException(status_code=500, detail="文件保存失败")

    # 更新真实文件大小和最终时间
    await db.execute(
        "UPDATE files SET size = ?, upload_time = ? WHERE code = ?",
        (file_size, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code)
    )
    await db.commit()
    await db.close()

    return {
        "code": code,
        "filename": original_filename,
        "size": file_size,
        "upload_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

@app.get("/api/file/{code}")
async def get_file_info(code: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT filename, size, upload_time FROM files WHERE code = ?", (code,)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(status_code=404, detail="文件码不存在")
    return {
        "filename": row[0],
        "size": row[1],
        "upload_time": row[2]
    }

@app.get("/api/download/{code}")
async def download_file(code: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT filename FROM files WHERE code = ?", (code,))
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(status_code=404, detail="文件码不存在")

    found = None
    for fn in os.listdir(UPLOAD_DIR):
        if fn.startswith(code + ".") or fn == code:
            found = fn
            break
    if not found:
        raise HTTPException(status_code=404, detail="文件实体不存在")

    file_path = os.path.join(UPLOAD_DIR, found)
    return FileResponse(
        path=file_path,
        filename=row[0],
        media_type="application/octet-stream"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)