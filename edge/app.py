import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional
import random

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
import uvicorn


# ============================================================================
# MODELS
# ============================================================================

class TelemetryMessage(BaseModel):
    device_id: str = Field(..., description="Unique device identifier")
    sequence_id: int = Field(..., description="Message sequence number")
    timestamp: str = Field(..., description="ISO timestamp from device")
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    pressure: Optional[float] = None


class IngestResponse(BaseModel):
    status: str
    message: str
    correlation_id: str


# ============================================================================
# IN-MEMORY CACHE FOR IDEMPOTENCY
# ============================================================================

class MessageCache:
    """TTL-based in-memory cache for seen messages."""
    
    def __init__(self, ttl_seconds: int = 300):
        self.cache: Dict[str, float] = {}  # key -> expiry_timestamp
        self.ttl_seconds = ttl_seconds
    
    def _cleanup(self):
        """Remove expired entries."""
        now = time.time()
        expired = [k for k, v in self.cache.items() if v < now]
        for k in expired:
            del self.cache[k]
    
    def has_seen(self, device_id: str, sequence_id: int) -> bool:
        """Check if message was already processed."""
        self._cleanup()
        key = f"{device_id}:{sequence_id}"
        return key in self.cache
    
    def mark_seen(self, device_id: str, sequence_id: int):
        """Mark message as processed."""
        key = f"{device_id}:{sequence_id}"
        self.cache[key] = time.time() + self.ttl_seconds


# ============================================================================
# METRICS
# ============================================================================

class Metrics:
    """Simple in-memory metrics counters."""
    
    def __init__(self):
        self.received_total = 0
        self.accepted_total = 0
        self.duplicates_total = 0
        self.rejected_total = 0
        self.transient_503_total = 0
    
    def to_dict(self) -> dict:
        return {
            "received_total": self.received_total,
            "accepted_total": self.accepted_total,
            "duplicates_total": self.duplicates_total,
            "rejected_total": self.rejected_total,
            "transient_503_total": self.transient_503_total,
        }


# ============================================================================
# APP INITIALIZATION
# ============================================================================

app = FastAPI(title="Edge Telemetry Ingestion Service", version="1.0.0")

message_cache = MessageCache(ttl_seconds=300)
metrics = Metrics()

# Overload simulation: randomly return 503 to test device retry logic
OVERLOAD_PROBABILITY = 0.1  # 10% chance of simulated overload


# ============================================================================
# STRUCTURED LOGGING
# ============================================================================

def log_event(
    level: str,
    event: str,
    correlation_id: str,
    device_id: Optional[str] = None,
    sequence_id: Optional[int] = None,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
):
    """Emit structured JSON log."""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "event": event,
        "correlation_id": correlation_id,
    }
    if device_id:
        log_entry["device_id"] = device_id
    if sequence_id is not None:
        log_entry["sequence_id"] = sequence_id
    if decision:
        log_entry["decision"] = decision
    if reason:
        log_entry["reason"] = reason
    
    print(log_entry)  # In production, use proper JSON logger


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat() + "Z"}


@app.get("/metrics")
def get_metrics():
    """Return current metrics."""
    return metrics.to_dict()


@app.post("/ingest", response_model=IngestResponse)
def ingest_telemetry(msg: TelemetryMessage, request: Request):
    """
    Ingest telemetry from devices.
    
    - Returns 200 for accepted messages
    - Returns 409 for duplicates (idempotent)
    - Returns 400 for malformed/invalid messages (non-transient)
    - Returns 503 for simulated overload (transient)
    """
    correlation_id = str(uuid.uuid4())
    
    metrics.received_total += 1
    
    log_event(
        level="INFO",
        event="telemetry_received",
        correlation_id=correlation_id,
        device_id=msg.device_id,
        sequence_id=msg.sequence_id,
    )
    
    # SIMULATE OVERLOAD (transient error - device should retry)
    if random.random() < OVERLOAD_PROBABILITY:
        metrics.transient_503_total += 1
        log_event(
            level="WARN",
            event="overload_simulated",
            correlation_id=correlation_id,
            device_id=msg.device_id,
            sequence_id=msg.sequence_id,
            decision="rejected_transient",
            reason="simulated_backpressure",
        )
        raise HTTPException(status_code=503, detail="Service temporarily overloaded")
    
    # BASIC VALIDATION (non-transient errors - device should NOT retry)
    if msg.sequence_id < 0:
        metrics.rejected_total += 1
        log_event(
            level="ERROR",
            event="validation_failed",
            correlation_id=correlation_id,
            device_id=msg.device_id,
            sequence_id=msg.sequence_id,
            decision="rejected",
            reason="invalid_sequence_id",
        )
        raise HTTPException(status_code=400, detail="sequence_id must be >= 0")
    
    # IDEMPOTENCY CHECK
    if message_cache.has_seen(msg.device_id, msg.sequence_id):
        metrics.duplicates_total += 1
        log_event(
            level="INFO",
            event="duplicate_detected",
            correlation_id=correlation_id,
            device_id=msg.device_id,
            sequence_id=msg.sequence_id,
            decision="duplicate",
            reason="already_processed",
        )
        # Return 409 Conflict for duplicates (not an error, but not re-processed)
        raise HTTPException(status_code=409, detail="Duplicate message")
    
    # ACCEPT MESSAGE
    message_cache.mark_seen(msg.device_id, msg.sequence_id)
    metrics.accepted_total += 1
    
    log_event(
        level="INFO",
        event="telemetry_accepted",
        correlation_id=correlation_id,
        device_id=msg.device_id,
        sequence_id=msg.sequence_id,
        decision="accepted",
        reason="new_message",
    )
    
    return IngestResponse(
        status="accepted",
        message="Telemetry ingested successfully",
        correlation_id=correlation_id,
    )


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")