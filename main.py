import io
import json
import logging
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from data_pipeline import create_cleaning_report, process_data, validate_dataframe
from src import (
    DatasetInfo,
    DatasetStorage,
    HealthResponse,
    ProcessConfig,
    ProcessingReport,
    log_requests,
    setup_logging,
)

setup_logging(verbose=True)
logger = logging.getLogger(__name__)

app = FastAPI(title="Data Processing API", version="1.0.0")
app.middleware("http")(log_requests)
storage = DatasetStorage()


@app.get("/health", response_model=HealthResponse)
def health_check():
    """Return the application status and version."""
    return HealthResponse(status="ok", version=app.version)


@app.post("/upload", response_model=DatasetInfo)
async def upload_dataset(file: UploadFile = File(...)):
    """Read and store a CSV upload."""
    filename = file.filename or ""
    extension = Path(filename).suffix.lower()
    if extension != ".csv":
        shown = extension or "(no extension)"
        logger.warning("Unsupported file format: %s", shown)
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format {shown}. Please upload a .csv file.",
        )

    contents = await file.read()
    try:
        df = pd.read_csv(io.BytesIO(contents))
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as error:
        logger.warning("Could not read CSV upload %s: %s", filename, error)
        raise HTTPException(
            status_code=400,
            detail=f"Could not read {filename} as a CSV file: {error}",
        ) from error

    dataset_id = storage.save(df, filename)
    logger.info(
        "Uploaded dataset: id=%s, filename=%s, rows=%s",
        dataset_id,
        filename,
        len(df),
    )
    return DatasetInfo(**storage.get_metadata(dataset_id))


@app.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: int, limit: int = Query(default=100, ge=1)):
    """Return up to limit rows from a stored dataset."""
    df = storage.get(dataset_id)
    if df is None:
        logger.warning("Dataset not found: %s", dataset_id)
        raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found.")

    records = json.loads(df.head(limit).to_json(orient="records"))
    logger.info("Retrieved dataset: id=%s, returned_rows=%s", dataset_id, len(records))
    return {"id": dataset_id, "data": records, "total_rows": len(df)}


@app.post(
    "/datasets/{dataset_id}/process",
    response_model=ProcessingReport
)
def process_dataset(dataset_id: int, config: ProcessConfig):
    """Validate and process a stored dataset using MP1 functions."""
    # 1. Retrieve a copy (missing-dataset check stays outside the try block)
    df = storage.get(dataset_id)
    if df is None:
        logger.warning("Dataset not found: %s", dataset_id)
        raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found.")

    # 2. Convert the request model to the MP1-style config dictionary
    config_dict = config.model_dump()
    required_columns = config_dict["validation"]["required_columns"]
    numeric_columns = config_dict["validation"]["numeric_columns"]

    # 3. Every configured numeric column must exist
    missing_numeric = [column for column in numeric_columns if column not in df.columns]
    if missing_numeric:
        logger.warning(
            "Numeric column(s) not found: id=%s, columns=%s",
            dataset_id,
            missing_numeric,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Numeric column(s) not found in dataset: {', '.join(missing_numeric)}",
        )

    try:
        # 4-5. Validate and record how many rows validation removed
        rows_before_validation = len(df)
        validated = validate_dataframe(df, required_columns, numeric_columns)
        validation_rows_removed = rows_before_validation - len(validated)

        # 6-7. Keep a copy of the validated data, process, then compare
        validated_copy = validated.copy()
        processed = process_data(validated, config_dict)
        cleaning_report = create_cleaning_report(validated_copy, processed)
    except ValueError as error:
        logger.warning("Processing failed: id=%s, error=%s", dataset_id, error)
        raise HTTPException(status_code=400, detail=str(error)) from error

    # 8. Only reached if validation and processing both succeeded
    storage.update(dataset_id, processed)
    logger.info(
        "Processed dataset: id=%s, validation_removed=%s, rows_before=%s, rows_after=%s",
        dataset_id,
        validation_rows_removed,
        len(validated_copy),
        len(processed),
    )

    # 9. Matches the ProcessingReport response model
    return {
        "id": dataset_id,
        "validation_rows_removed": validation_rows_removed,
        "cleaning_report": cleaning_report,
    }