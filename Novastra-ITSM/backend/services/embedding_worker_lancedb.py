# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: GPU-accelerated embedding worker for LanceDB vector store.
# Date: 2026-01-27
# ---------------------------------------------------------------------------
"""GPU-accelerated embedding worker for LanceDB vector store."""
from __future__ import annotations

import gc
import logging
import re
import uuid
from typing import Any

import backend.config as cfg
from backend.services.lancedb_store import lancedb_enabled, upsert_points as lancedb_upsert_points

logger = logging.getLogger(__name__)


# Function: _clean_text
def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "nan"}:
        return ""
    return text


# Function: _get_gpu_embeddings
def _get_gpu_embeddings() -> Any:
    """Get GPU-optimized embedding model using local models."""
    try:
        # Try using HuggingFace embeddings with GPU support
        from sentence_transformers import SentenceTransformer
        
        model_name = cfg.EMBEDDING_MODEL or "nomic-embed-text"
        device = cfg.EMBEDDING_DEVICE or "cuda"
        
        logger.info("Loading embedding model '%s' on device '%s'", model_name, device)
        
        model = SentenceTransformer(model_name, device=device)
        logger.info("GPU embedding model loaded successfully")
        return model
        
    except Exception as exc:
        logger.warning("GPU embedding model unavailable (%s), falling back to Ollama", exc)
        return None


# Function: _get_fallback_embeddings
def _get_fallback_embeddings() -> Any:
    """Fallback to OpenAI or Ollama embeddings."""
    if cfg.OPENAI_API_KEY:
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(api_key=cfg.OPENAI_API_KEY)

    # Fall back to Ollama. client_kwargs sets a request timeout — without one, a
    # wedged Ollama embedding worker (seen live: /api/embed hangs while /api/generate
    # keeps responding normally) hangs this call forever, which in turn hangs
    # whatever HTTP request triggered it (e.g. a dashboard Save or Omnichannel
    # simulate) instead of failing fast and reporting a clean error.
    from langchain_ollama import OllamaEmbeddings
    return OllamaEmbeddings(
        model=cfg.OLLAMA_EMBED_MODEL,
        base_url=cfg.OLLAMA_BASE_URL,
        client_kwargs={"timeout": 30.0},
    )


# Function: _get_embeddings
def _get_embeddings() -> Any:
    """Get embeddings - try GPU first, then fallback."""
    if cfg.GPU_ENABLED:
        gpu_model = _get_gpu_embeddings()
        if gpu_model is not None:
            return gpu_model
    
    return _get_fallback_embeddings()


# Function: _incident_text
def _incident_text(record: dict) -> str:
    """Build text representation of incident."""
    return "\n".join(
        [
            f"Incident Number: {_clean_text(record.get('number'))}",
            f"Short Description: {_clean_text(record.get('short_description'))}",
            f"Description: {_clean_text(record.get('description'))}",
            f"Category: {_clean_text(record.get('category'))}",
            f"Subcategory: {_clean_text(record.get('subcategory'))}",
            f"State: {_clean_text(record.get('state'))}",
            f"Priority: {_clean_text(record.get('priority'))}",
            f"Assignment Group: {_clean_text(record.get('assignment_group'))}",
            f"Assigned To: {_clean_text(record.get('assigned_to'))}",
            f"Work Notes: {_clean_text(record.get('work_notes'))}",
            f"Close Notes: {_clean_text(record.get('close_notes'))}",
        ]
    )


# Function: _chunk_text
def _chunk_text(text: str) -> list[str]:
    """Split text into chunks."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return []

    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=max(500, int(cfg.CHUNK_SIZE)),
            chunk_overlap=max(50, int(cfg.CHUNK_OVERLAP)),
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        return [c.strip() for c in splitter.split_text(compact) if c.strip()]
    except Exception:
        # Fallback: simple chunking
        out: list[str] = []
        max_len = max(500, int(cfg.CHUNK_SIZE))
        i = 0
        while i < len(compact):
            out.append(compact[i : i + max_len])
            i += max_len
        return out


# Function: _stable_point_id
def _stable_point_id(ticket_id: str, chunk_idx: int, source_name: str) -> str:
    """Generate deterministic ID for a chunk."""
    key = f"{ticket_id}|{chunk_idx}|{source_name}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


# Function: _embed_batch
def _embed_batch(texts: list[str], embedding_model: Any) -> list[list[float]]:
    """Embed a batch of texts using the model."""
    try:
        # If it's a SentenceTransformer model
        if hasattr(embedding_model, 'encode'):
            embeddings = embedding_model.encode(
                texts,
                batch_size=cfg.EMBEDDING_BATCH_SIZE,
                show_progress_bar=False,
                convert_to_tensor=False,
            )
            return [emb.tolist() if hasattr(emb, 'tolist') else list(emb) for emb in embeddings]
        
        # If it's a LangChain embedding model
        elif hasattr(embedding_model, 'embed_documents'):
            return embedding_model.embed_documents(texts)
        
        else:
            raise ValueError(f"Unknown embedding model type: {type(embedding_model)}")
            
    except Exception as exc:
        logger.error("Embedding failed: %s", exc)
        raise


# Function: _iter_complete_incident_batches
def _iter_complete_incident_batches(
    chunks: list[tuple[str, str, dict[str, Any]]],
    target_size: int,
):
    """Yield bounded batches without splitting an incident across writes."""
    grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk[0], []).append(chunk)

    pending: list[tuple[str, str, dict[str, Any]]] = []
    for incident_chunks in grouped.values():
        if pending and len(pending) + len(incident_chunks) > target_size:
            yield pending
            pending = []
        if len(incident_chunks) > target_size:
            yield incident_chunks
        else:
            pending.extend(incident_chunks)
    if pending:
        yield pending


# Function: index_incidents_to_lancedb
def index_incidents_to_lancedb(
    records: list[dict],
    source_name: str,
    batch_size: int | None = None
) -> int:
    """
    Index incidents into LanceDB with GPU-accelerated embeddings.
    
    Args:
        records: List of incident dictionaries from ServiceNow
        source_name: Name of the data source
        batch_size: Embedding batch size (defaults to config)
    
    Returns:
        Number of chunks indexed
    """
    if not records or not lancedb_enabled():
        return 0

    batch_size = batch_size or cfg.EMBEDDING_BATCH_SIZE

    # Prepare chunks
    chunks: list[tuple[str, str, dict[str, Any]]] = []
    
    for row in records:
        ticket_id = _clean_text(row.get("number")) or _clean_text(row.get("sys_id"))
        if not ticket_id:
            continue

        category = _clean_text(row.get("category"))
        state = _clean_text(row.get("state"))
        group = _clean_text(row.get("assignment_group"))
        short_description = _clean_text(row.get("short_description"))

        text = _incident_text(row)
        text_chunks = _chunk_text(text)
        
        for idx, chunk in enumerate(text_chunks):
            payload = {
                "ticket_id": ticket_id,
                "source_type": "incident",
                "short_description": short_description,
                "description_chunk": chunk,
                "category": category,
                "state": state,
                "group": group,
                "source_name": source_name,
                "chunk_index": idx,
            }
            chunks.append((ticket_id, chunk, payload))

    if not chunks:
        logger.info("No chunks to index for source '%s'", source_name)
        return 0

    logger.info("Indexing %d chunks for source '%s' into LanceDB", len(chunks), source_name)

    # Get embedding model
    embedding_model = _get_embeddings()
    logger.info("Using embedding model: %s", type(embedding_model).__name__)

    # For LangChain/Ollama models use OLLAMA_EMBED_BATCH_SIZE (150) to reduce HTTP round-trips.
    # SentenceTransformer's encode() handles its own GPU batching internally, so for ST models
    # a single large call is fine — the GPU batch size is controlled inside _embed_batch().
    if hasattr(embedding_model, 'embed_documents') and not hasattr(embedding_model, 'encode'):
        effective_batch = max(1, int(getattr(cfg, 'OLLAMA_EMBED_BATCH_SIZE', 150)))
    else:
        effective_batch = batch_size

    # Embed and persist complete incident groups before moving on. The previous
    # implementation retained every 768-float vector and then duplicated them in
    # a points list, which could exceed process memory for large baselines. Keeping
    # each incident in one write also preserves replace semantics without deleting
    # the full existing index up front.
    inserted = 0
    point_sequence = 0
    for batch_number, batch_chunks in enumerate(
        _iter_complete_incident_batches(chunks, effective_batch),
        start=1,
    ):
        batch_texts = [chunk[1] for chunk in batch_chunks]
        logger.debug("Embedding batch %d (%d texts)", batch_number, len(batch_texts))
        batch_vectors = _embed_batch(batch_texts, embedding_model)
        if len(batch_vectors) != len(batch_chunks):
            raise RuntimeError(
                f"Embedding provider returned {len(batch_vectors)} vectors for {len(batch_chunks)} chunks."
            )
        points = []
        for local_idx, ((ticket_id, _chunk, payload), vector) in enumerate(zip(batch_chunks, batch_vectors)):
            points.append(
                {
                    "id": _stable_point_id(ticket_id, point_sequence + local_idx, source_name),
                    "vector": [float(x) for x in vector],
                    "payload": payload,
                }
            )
        inserted += lancedb_upsert_points(points)
        point_sequence += len(points)
        del batch_vectors, points, batch_chunks, batch_texts

    del embedding_model, chunks
    gc.collect()

    logger.info(
        "Successfully indexed %d chunks into LanceDB table '%s' from source '%s'",
        inserted,
        cfg.LANCEDB_TABLE,
        source_name
    )
    
    # Verify persistence by checking table stats
    try:
        from backend.services.lancedb_store import get_table_stats
        stats = get_table_stats()
        logger.info("LanceDB persistence verification: %s", stats)
    except Exception as exc:
        logger.warning("Could not verify LanceDB persistence: %s", exc)
    
    return inserted


# Function: index_incidents_to_lancedb_batch
def index_incidents_to_lancedb_batch(
    records: list[dict],
    source_name: str,
) -> dict[str, Any]:
    """
    Index incidents with detailed statistics.
    
    Returns:
        Dictionary with indexing statistics
    """
    try:
        indexed = index_incidents_to_lancedb(records, source_name)
        return {
            "success": True,
            "indexed_chunks": indexed,
            "source_name": source_name,
            "vector_backend": "lancedb",
            "embedding_model": cfg.EMBEDDING_MODEL,
            "device": cfg.EMBEDDING_DEVICE,
        }
    except Exception as exc:
        logger.error("Failed to index incidents: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "source_name": source_name,
            "vector_backend": "lancedb",
        }
