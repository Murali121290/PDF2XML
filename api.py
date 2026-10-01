import shutil
import tempfile
from pathlib import Path
from typing import List

import uvicorn
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, FileResponse

from pdf2xml.config import Config
from pdf2xml.pipeline import convert as run_pipeline
from pdf2xml.pipeline import ConversionError

app = FastAPI(
    title="pdf2xml API",
    description="FastAPI wrapper for the pdf2xml conversion pipeline.",
    version="0.3.1",
)

# You can adjust this to a permanent directory if you want to keep the results
OUTPUT_ROOT = Path("out")

@app.get("/")
def read_root():
    return {"message": "pdf2xml API is running. Use POST /convert to convert PDFs."}

@app.post("/convert")
async def convert_pdf(
    file: UploadFile = File(...),
    engine: str = "heuristic",  # auto, docling, heuristic
    targets: str = "json,xml,epub,docx", 
    return_xml: str | None = None, # e.g. 'jats', 'bits', 'canonical'
):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    # Parse targets
    target_list = [t.strip() for t in targets.split(",") if t.strip()]

    # Create output directory
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    
    # Save uploaded file to a temporary location
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_pdf_path = Path(tmp.name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {e}")

    try:
        cfg = Config(
            engine=engine,
            targets=target_list,
            overlays=False,
            reuse_cache=False,
        )
        
        # Run conversion
        res = run_pipeline(temp_pdf_path, OUTPUT_ROOT, cfg)
        
        # Clean up temporary PDF file
        temp_pdf_path.unlink()

        if return_xml:
            xml_file_map = {
                "jats": "41_jats.xml",
                "bits": "40_bits.xml",
                "canonical": "30_canonical.xml"
            }
            if return_xml in xml_file_map:
                xml_path = res.out_dir / xml_file_map[return_xml]
                if xml_path.exists():
                    return FileResponse(path=xml_path, media_type="application/xml", filename=xml_path.name)
                else:
                    raise HTTPException(status_code=404, detail=f"{return_xml} XML not found. Ensure it is included in targets.")
            else:
                raise HTTPException(status_code=400, detail="return_xml must be 'jats', 'bits', or 'canonical'")

        return JSONResponse(content={
            "status": "success",
            "filename": file.filename,
            "output_directory": str(res.out_dir.absolute()),
            "blocks": len(res.document.body),
            "coverage": res.coverage.ratio,
            "xml_valid": not res.xml_errors,
        })
    except ConversionError as e:
        if temp_pdf_path.exists():
            temp_pdf_path.unlink()
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        if temp_pdf_path.exists():
            temp_pdf_path.unlink()
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
