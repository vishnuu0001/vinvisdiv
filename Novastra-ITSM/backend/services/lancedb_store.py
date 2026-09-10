# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: LanceDB integration for local GPU-accelerated vector storage.
# Date: 2025-08-10
# ---------------------------------------------------------------------------
"""LanceDB integration for local GPU-accelerated vector storage."""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import backend.config as cfg

logger = logging.getLogger(__name__)


class LanceDBUnavailableError(RuntimeError):
    pass


# Function: lancedb_enabled
def lancedb_enabled() -> bool:
    return cfg.VECTOR_BACKEND in {"lancedb", "hybrid"} and bool((cfg.LANCEDB_PATH or "").strip())


# Function: get_lancedb_client
@lru_cache(maxsize=1)
def get_lancedb_client():
    """Get or create LanceDB client."""
    if not lancedb_enabled():
        raise LanceDBUnavailableError("LanceDB backend is disabled by configuration.")

    try:
        import lancedb
    except Exception as exc:
        raise LanceDBUnavailableError(
            "lancedb is not installed. Add lancedb to backend requirements."
        ) from exc

    db_path = cfg.LANCEDB_PATH
    if not db_path:
        raise LanceDBUnavailableError("LANCEDB_PATH not configured")
    
    # Ensure directory exists
    os.makedirs(db_path, exist_ok=True)
    
    client = lancedb.connect(db_path)
    logger.info("LanceDB client initialized at: %s", db_path)
    return client


# Function: ensure_table
def ensure_table(vector_size: int) -> Any:
    """Ensure LanceDB table exists with proper schema."""
    if vector_size <= 0:
        raise ValueError("vector_size must be greater than zero")

    client = get_lancedb_client()
    table_name = cfg.LANCEDB_TABLE
    
    try:
        # Check if table exists
        table = client.open_table(table_name)
        logger.info("LanceDB table '%s' already exists with %d rows", table_name, len(table))
        return table
    except Exception:
        # Table doesn't exist, will be created on first upsert
        logger.info("LanceDB table '%s' will be created on first insert", table_name)
        return None


# Function: upsert_points
def upsert_points(points: list[dict[str, Any]]) -> int:
    """
    Upsert points into LanceDB table with required schema:
    - incident_id: incident identifier
    - embedding: vector embedding
    - text_chunk: text chunk content
    - metadata: JSON object with additional fields
    
    Each point should have:
    - id: unique identifier
    - vector: embedding vector
    - payload: metadata dict with text chunk and incident data
    """
    if not points:
        return 0

    client = get_lancedb_client()
    table_name = cfg.LANCEDB_TABLE

    try:
        # Transform points to LanceDB format with required schema
        records = []
        for point in points:
            payload = point.get("payload", {})
            incident_id = payload.get("ticket_id", "")
            
            # Main required fields
            record = {
                "incident_id": incident_id,
                "embedding": [float(x) for x in point["vector"]],
                "text_chunk": payload.get("description_chunk", ""),
                # Metadata object with all related fields
                "metadata": {
                    "source_type": payload.get("source_type", "incident"),
                    "short_description": payload.get("short_description", ""),
                    "category": payload.get("category", ""),
                    "state": payload.get("state", ""),
                    "assignment_group": payload.get("group", ""),
                    "source_name": payload.get("source_name", ""),
                    "chunk_index": payload.get("chunk_index", 0),
                }
            }
            records.append(record)

        if not records:
            return 0

        if not _table_exists(client, table_name):
            # Create table on first insert using the first batch as schema
            table = client.create_table(table_name, data=records, mode="overwrite")
            logger.debug("Created new LanceDB table '%s' with %d initial records", table_name, len(records))
        else:
            table = client.open_table(table_name)
            # For updates, delete existing chunks for these incident_ids then re-add
            existing_incident_ids = {r["incident_id"] for r in records if r["incident_id"]}
            try:
                if existing_incident_ids:
                    id_list = ",".join(f"'{id_}'" for id_ in existing_incident_ids)
                    table.delete(f"incident_id IN ({id_list})")
            except Exception:
                pass
            # Add new records
            table.add(records)
        
        # CRITICAL: Force LanceDB to flush/persist data to disk
        # This ensures vectorstore/ directory contains actual Lance files, not just in-memory data
        try:
            if hasattr(client, 'close'):
                client.close()
            logger.debug("LanceDB client closed to ensure data persistence")
        except Exception as exc:
            logger.warning("Error closing LanceDB client (data may still be persisted): %s", exc)
        
        # Clear the cached client to force a fresh connection on next call
        get_lancedb_client.cache_clear()
        
        logger.info(
            "Upserted %d records into LanceDB table '%s' with schema: incident_id, embedding, text_chunk, metadata (persisted to disk)",
            len(records),
            table_name,
        )
        return len(records)

    except Exception as exc:
        raise RuntimeError(f"LanceDB upsert failed: {exc}") from exc


# Function: search_by_vector
def search_by_vector(vector: list[float], limit: int = 8) -> list[dict[str, Any]]:
    """Search LanceDB by vector similarity on the embedding field."""
    if not vector or not lancedb_enabled():
        return []

    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        # Check if table exists
        if not _table_exists(client, table_name):
            return []
        
        table = client.open_table(table_name)
        
        # Perform vector search on "embedding" field (must name column explicitly)
        results = table.search(vector, vector_column_name="embedding").limit(max(1, int(limit))).to_list()
        
        return results or []
        
    except Exception as exc:
        logger.warning("LanceDB search failed: %s", exc)
        return []


# Function: search_hybrid
def search_hybrid(vector: list[float], text_query: str = "", limit: int = 8) -> list[dict[str, Any]]:
    """Hybrid search combining vector and text search."""
    if not lancedb_enabled():
        return []
    
    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        if not _table_exists(client, table_name):
            return []
        
        table = client.open_table(table_name)
        
        # Start with vector search on the "embedding" column
        results = table.search(vector, vector_column_name="embedding").limit(max(1, int(limit))).to_list()
        
        # If text_query provided, also filter by text relevance
        if text_query and results:
            # LanceDB supports where clauses for filtering
            filtered = []
            for result in results:
                # Check if text_query appears in text_chunk or metadata.short_description
                chunk = str(result.get("text_chunk", "")).lower()
                metadata = result.get("metadata", {})
                desc = str(metadata.get("short_description", "")).lower()
                if text_query.lower() in chunk or text_query.lower() in desc:
                    filtered.append(result)
            
            # Return filtered results, or fall back to vector-only if filtering removes all
            return filtered if filtered else results
        
        return results or []
        
    except Exception as exc:
        logger.warning("LanceDB hybrid search failed: %s", exc)
        return []


# Function: get_table_stats
def get_table_stats() -> dict[str, Any]:
    """Get statistics about the LanceDB table."""
    if not lancedb_enabled():
        return {"error": "LanceDB backend disabled"}

    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        if not _table_exists(client, table_name):
            return {
                "table_name": table_name,
                "row_count": 0,
                "status": "table_does_not_exist",
            }
        
        table = client.open_table(table_name)
        row_count = len(table)
        
        # Get schema info
        schema = table.schema
        
        return {
            "table_name": table_name,
            "row_count": row_count,
            "schema": str(schema),
            "status": "ready",
        }
        
    except Exception as exc:
        logger.warning("Failed to get LanceDB stats: %s", exc)
        return {"error": str(exc), "status": "error"}


# Function: get_points_count
def get_points_count() -> int:
    """Get total number of vectors in LanceDB table."""
    if not lancedb_enabled():
        return 0

    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        if not _table_exists(client, table_name):
            return 0
        
        table = client.open_table(table_name)
        return len(table)
        
    except Exception:
        return 0


# Function: delete_by_ticket_id
def delete_by_ticket_id(ticket_id: str) -> int:
    """Delete all vectors associated with a ticket."""
    if not lancedb_enabled():
        return 0

    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        if not _table_exists(client, table_name):
            return 0
        
        table = client.open_table(table_name)

        # The stored column is incident_id (see upsert_points) — this used to filter
        # on a "ticket_id" column that doesn't exist in the schema, so every call
        # silently deleted nothing and logged success anyway.
        table.delete(f"incident_id = '{ticket_id}'")

        logger.info("Deleted vectors for ticket: %s", ticket_id)
        return 1
        
    except Exception as exc:
        logger.warning("Failed to delete vectors for ticket %s: %s", ticket_id, exc)
        return 0


# Function: clear_table
def clear_table() -> bool:
    """Clear all vectors from the LanceDB table."""
    if not lancedb_enabled():
        return False

    try:
        client = get_lancedb_client()
        table_name = cfg.LANCEDB_TABLE
        
        if _table_exists(client, table_name):
            table = client.open_table(table_name)
            table.delete("1=1")  # Delete all rows
            logger.info("Cleared LanceDB table '%s'", table_name)
        
        return True
        
    except Exception as exc:
        logger.error("Failed to clear LanceDB table: %s", exc)
        return False


# Function: _table_exists
def _table_exists(client: Any, table_name: str) -> bool:
    """Check if table exists in LanceDB."""
    try:
        client.open_table(table_name)
        return True
    except Exception:
        return False
