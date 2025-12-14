import time
import random
import json
import sys
from datetime import datetime
from typing import Optional
import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

EDGE_URL = "http://localhost:8000/ingest"
DEVICE_ID = "device-001"
TELEMETRY_INTERVAL_SECONDS = 3

# Network instability simulation probabilities
PACKET_DROP_PROBABILITY = 0.15  # 15% chance to drop packet (not send)
JITTER_PROBABILITY = 0.20       # 20% chance to add delay
MAX_JITTER_SECONDS = 2.0
DUPLICATE_PROBABILITY = 0.10    # 10% chance to send duplicate

# Retry configuration (exponential backoff)
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
BACKOFF_JITTER_RANGE = 0.5  # +/- 50% jitter on backoff


# ============================================================================
# STRUCTURED LOGGING
# ============================================================================

def log_event(level: str, event: str, **kwargs):
    """Emit structured JSON log from device."""
    log_entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "component": "device",
        "device_id": DEVICE_ID,
        "event": event,
    }
    log_entry.update(kwargs)
    print(json.dumps(log_entry))


# ============================================================================
# TELEMETRY GENERATION
# ============================================================================

def generate_telemetry(sequence_id: int) -> dict:
    """Generate random telemetry data."""
    return {
        "device_id": DEVICE_ID,
        "sequence_id": sequence_id,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "temperature": round(random.uniform(18.0, 28.0), 2),
        "humidity": round(random.uniform(30.0, 70.0), 2),
        "pressure": round(random.uniform(980.0, 1020.0), 2),
    }


# ============================================================================
# NETWORK INSTABILITY SIMULATION
# ============================================================================

def simulate_packet_drop() -> bool:
    """Randomly drop packets."""
    if random.random() < PACKET_DROP_PROBABILITY:
        log_event("WARN", "packet_dropped", reason="simulated_network_instability")
        return True
    return False


def simulate_jitter():
    """Add random network delay."""
    if random.random() < JITTER_PROBABILITY:
        delay = random.uniform(0.1, MAX_JITTER_SECONDS)
        log_event("INFO", "jitter_applied", delay_seconds=round(delay, 2))
        time.sleep(delay)


def simulate_duplicate() -> bool:
    """Decide if message should be sent twice."""
    if random.random() < DUPLICATE_PROBABILITY:
        log_event("WARN", "duplicate_triggered", reason="simulated_network_instability")
        return True
    return False


# ============================================================================
# RETRY LOGIC WITH EXPONENTIAL BACKOFF
# ============================================================================

def calculate_backoff(attempt: int) -> float:
    """Calculate exponential backoff with jitter."""
    backoff = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
    jitter = backoff * random.uniform(-BACKOFF_JITTER_RANGE, BACKOFF_JITTER_RANGE)
    return max(0.1, backoff + jitter)


def is_transient_error(status_code: Optional[int], exception: Optional[Exception]) -> bool:
    """
    Determine if error is transient (should retry) or permanent (should not retry).
    
    Transient errors (retry):
    - 429 Too Many Requests
    - 503 Service Unavailable
    - Network timeouts
    - Connection errors
    
    Non-transient errors (don't retry):
    - 400 Bad Request (validation error)
    - 409 Conflict (duplicate - already processed)
    - 401/403 (authentication/authorization)
    """
    if exception:
        # Network-level errors are transient
        if isinstance(exception, (requests.exceptions.Timeout, 
                                   requests.exceptions.ConnectionError)):
            return True
        return False
    
    if status_code:
        # Transient HTTP errors
        if status_code in [429, 503, 504]:
            return True
        # Non-transient errors
        if status_code in [400, 401, 403, 409]:
            return False
    
    return False


def send_with_retry(telemetry: dict) -> bool:
    """
    Send telemetry with retry logic for transient errors.
    
    Returns True if successfully sent, False if permanently failed.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            log_event(
                "INFO",
                "sending_telemetry",
                sequence_id=telemetry["sequence_id"],
                attempt=attempt + 1,
            )
            
            response = requests.post(
                EDGE_URL,
                json=telemetry,
                timeout=5.0,
            )
            
            # Success
            if response.status_code == 200:
                log_event(
                    "INFO",
                    "telemetry_sent_success",
                    sequence_id=telemetry["sequence_id"],
                    status_code=response.status_code,
                    correlation_id=response.json().get("correlation_id"),
                )
                return True
            
            # Duplicate (409) - already processed, consider success
            if response.status_code == 409:
                log_event(
                    "INFO",
                    "duplicate_acknowledged",
                    sequence_id=telemetry["sequence_id"],
                    status_code=response.status_code,
                    reason="message_already_processed",
                )
                return True
            
            # Check if error is transient
            if is_transient_error(response.status_code, None):
                log_event(
                    "WARN",
                    "transient_error_received",
                    sequence_id=telemetry["sequence_id"],
                    status_code=response.status_code,
                    attempt=attempt + 1,
                )
                
                if attempt < MAX_RETRIES:
                    backoff = calculate_backoff(attempt)
                    log_event(
                        "INFO",
                        "retrying_after_backoff",
                        backoff_seconds=round(backoff, 2),
                        attempt=attempt + 1,
                    )
                    time.sleep(backoff)
                    continue
                else:
                    log_event(
                        "ERROR",
                        "max_retries_exceeded",
                        sequence_id=telemetry["sequence_id"],
                        status_code=response.status_code,
                    )
                    return False
            else:
                # Non-transient error - don't retry
                log_event(
                    "ERROR",
                    "non_transient_error",
                    sequence_id=telemetry["sequence_id"],
                    status_code=response.status_code,
                    reason="validation_or_auth_error",
                )
                return False
        
        except requests.exceptions.Timeout:
            log_event(
                "WARN",
                "request_timeout",
                sequence_id=telemetry["sequence_id"],
                attempt=attempt + 1,
            )
            
            if attempt < MAX_RETRIES:
                backoff = calculate_backoff(attempt)
                log_event("INFO", "retrying_after_backoff", backoff_seconds=round(backoff, 2))
                time.sleep(backoff)
                continue
            else:
                log_event("ERROR", "max_retries_exceeded_timeout", sequence_id=telemetry["sequence_id"])
                return False
        
        except requests.exceptions.ConnectionError as e:
            log_event(
                "WARN",
                "connection_error",
                sequence_id=telemetry["sequence_id"],
                attempt=attempt + 1,
                error=str(e),
            )
            
            if attempt < MAX_RETRIES:
                backoff = calculate_backoff(attempt)
                log_event("INFO", "retrying_after_backoff", backoff_seconds=round(backoff, 2))
                time.sleep(backoff)
                continue
            else:
                log_event("ERROR", "max_retries_exceeded_connection", sequence_id=telemetry["sequence_id"])
                return False
        
        except Exception as e:
            log_event(
                "ERROR",
                "unexpected_error",
                sequence_id=telemetry["sequence_id"],
                error=str(e),
            )
            return False
    
    return False


# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    """Main device loop."""
    sequence_id = 0
    
    log_event("INFO", "device_starting", edge_url=EDGE_URL)
    
    try:
        while True:
            telemetry = generate_telemetry(sequence_id)
            
            # Simulate packet drop
            if simulate_packet_drop():
                sequence_id += 1
                time.sleep(TELEMETRY_INTERVAL_SECONDS)
                continue
            
            # Simulate jitter (network delay)
            simulate_jitter()
            
            # Send telemetry
            send_with_retry(telemetry)
            
            # Simulate duplicate send
            if simulate_duplicate():
                time.sleep(0.5)  # Small delay before duplicate
                send_with_retry(telemetry)
            
            sequence_id += 1
            time.sleep(TELEMETRY_INTERVAL_SECONDS)
    
    except KeyboardInterrupt:
        log_event("INFO", "device_stopping", reason="user_interrupt")
        sys.exit(0)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()