"""
Qdrant Cloud client wrapper for Jacked.

Uses Qdrant Cloud Inference for server-side embedding.
"""

import logging
from typing import Optional, Any

from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.exceptions import UnexpectedResponse

from jacked.config import SmartForkConfig


logger = logging.getLogger(__name__)

# Qdrant Cloud Inference model - sentence-transformers/all-minilm-l6-v2 is fast and good
INFERENCE_MODEL = "sentence-transformers/all-minilm-l6-v2"
VECTOR_SIZE = 384  # MiniLM dimension


class QdrantSessionClient:
    """
    Client for interacting with Qdrant Cloud for session storage.

    Uses Qdrant Cloud Inference for server-side embedding - we send text
    and Qdrant embeds it on their servers.

    Attributes:
        config: SmartForkConfig instance
        client: Qdrant client instance

    Examples:
        >>> config = SmartForkConfig.from_env()  # doctest: +SKIP
        >>> client = QdrantSessionClient(config)  # doctest: +SKIP
        >>> client.ensure_collection()  # doctest: +SKIP
    """

    def __init__(self, config: SmartForkConfig):
        """
        Initialize the Qdrant client with Cloud Inference enabled.

        Args:
            config: SmartForkConfig instance with connection details
        """
        self.config = config
        self.client = QdrantClient(
            url=config.qdrant_endpoint,
            api_key=config.qdrant_api_key,
            timeout=60,
            cloud_inference=True,  # Enable server-side embedding
        )

    def ensure_collection(self) -> bool:
        """
        Ensure the collection exists, creating it if necessary.

        Creates collection with:
        - Dense vectors for semantic search
        - Payload indexing for repo filtering

        Returns:
            True if collection exists or was created

        Raises:
            Exception: If collection creation fails
        """
        collection_name = self.config.collection_name

        try:
            # Check if collection exists
            collections = self.client.get_collections()
            exists = any(c.name == collection_name for c in collections.collections)

            if exists:
                logger.info(f"Collection '{collection_name}' already exists")
                return True

            logger.info(f"Creating collection '{collection_name}'")

            # Create collection with dense vector config
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=VECTOR_SIZE,
                    distance=models.Distance.COSINE,
                    on_disk=True,
                ),
            )

            # Create payload indexes for efficient filtering
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="repo_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="repo_name",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="session_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="type",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="machine",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="user_name",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="content_type",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

            logger.info(f"Collection '{collection_name}' created successfully")
            return True

        except UnexpectedResponse as e:
            logger.error(f"Failed to create collection: {e}")
            raise

    def upsert_points(self, points: list[models.PointStruct]) -> bool:
        """
        Upsert points to the collection.

        Points should have vector=models.Document(text=..., model=...) for
        server-side embedding.

        Args:
            points: List of PointStruct objects to upsert

        Returns:
            True if successful
        """
        if not points:
            return True

        try:
            self.client.upsert(
                collection_name=self.config.collection_name,
                points=points,
                wait=True,
            )
            logger.debug(f"Upserted {len(points)} points")
            return True
        except UnexpectedResponse as e:
            logger.error(f"Failed to upsert points: {e}")
            raise

    def delete_by_session(self, session_id: str) -> bool:
        """
        Delete all points for a session.

        Args:
            session_id: Session ID to delete

        Returns:
            True if successful
        """
        try:
            self.client.delete(
                collection_name=self.config.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="session_id",
                                match=models.MatchValue(value=session_id),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"Deleted all points for session {session_id}")
            return True
        except UnexpectedResponse as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            raise

    def delete_by_user(self, user_name: str) -> int:
        """
        Delete all points for a specific user.

        Args:
            user_name: User name to delete data for

        Returns:
            Number of points deleted

        Examples:
            >>> client.delete_by_user("jack")  # doctest: +SKIP
            42
        """
        try:
            # First count points to delete
            count_result = self.client.count(
                collection_name=self.config.collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_name",
                            match=models.MatchValue(value=user_name),
                        )
                    ]
                ),
            )
            count = count_result.count

            if count == 0:
                logger.info(f"No points found for user {user_name}")
                return 0

            # Delete all points for this user
            self.client.delete(
                collection_name=self.config.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="user_name",
                                match=models.MatchValue(value=user_name),
                            )
                        ]
                    )
                ),
            )
            logger.info(f"Deleted {count} points for user {user_name}")
            return count
        except UnexpectedResponse as e:
            logger.error(f"Failed to delete data for user {user_name}: {e}")
            raise

    def count_by_user(self, user_name: str) -> int:
        """
        Count points for a specific user.

        Args:
            user_name: User name to count

        Returns:
            Number of points

        Examples:
            >>> client.count_by_user("jack")  # doctest: +SKIP
            42
        """
        try:
            result = self.client.count(
                collection_name=self.config.collection_name,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_name",
                            match=models.MatchValue(value=user_name),
                        )
                    ]
                ),
            )
            return result.count
        except UnexpectedResponse as e:
            logger.error(f"Failed to count for user {user_name}: {e}")
            raise

    def search(
        self,
        query_text: str,
        repo_id: Optional[str] = None,
        point_type: Optional[str] = None,
        content_types: Optional[list[str]] = None,
        user_name: Optional[str] = None,
        limit: int = 10,
    ) -> list[models.ScoredPoint]:
        """
        Search for similar points using server-side embedding.

        Args:
            query_text: Text to search for (will be embedded server-side)
            repo_id: Optional repo ID to filter by
            point_type: Optional point type filter (legacy, use content_types instead)
            content_types: Optional list of content types to search
                          (plan, subagent_summary, summary_label, user_message, chunk)
            user_name: Optional user name to filter by
            limit: Maximum number of results

        Returns:
            List of ScoredPoint objects
        """
        filter_conditions = []

        if repo_id:
            filter_conditions.append(
                models.FieldCondition(
                    key="repo_id",
                    match=models.MatchValue(value=repo_id),
                )
            )

        if point_type:
            # Legacy support
            filter_conditions.append(
                models.FieldCondition(
                    key="type",
                    match=models.MatchValue(value=point_type),
                )
            )

        if content_types:
            # Filter by content_type (supports multiple)
            filter_conditions.append(
                models.FieldCondition(
                    key="content_type",
                    match=models.MatchAny(any=content_types),
                )
            )

        if user_name:
            filter_conditions.append(
                models.FieldCondition(
                    key="user_name",
                    match=models.MatchValue(value=user_name),
                )
            )

        query_filter = None
        if filter_conditions:
            query_filter = models.Filter(must=filter_conditions)

        try:
            # Use query_points with Document for server-side embedding
            results = self.client.query_points(
                collection_name=self.config.collection_name,
                query=models.Document(
                    text=query_text,
                    model=INFERENCE_MODEL,
                ),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return results.points
        except UnexpectedResponse as e:
            logger.error(f"Search failed: {e}")
            raise

    def get_points_by_session(self, session_id: str) -> list[models.Record]:
        """
        Get all points for a session.

        Args:
            session_id: Session ID to retrieve

        Returns:
            List of Record objects
        """
        try:
            results, _ = self.client.scroll(
                collection_name=self.config.collection_name,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="session_id",
                            match=models.MatchValue(value=session_id),
                        )
                    ]
                ),
                limit=1000,
                with_payload=True,
                with_vectors=False,
            )
            return results
        except UnexpectedResponse as e:
            logger.error(f"Failed to get points for session {session_id}: {e}")
            raise

    def get_point_by_id(self, point_id: str) -> Optional[models.Record]:
        """
        Get a single point by ID.

        Args:
            point_id: Point ID to retrieve

        Returns:
            Record object or None if not found
        """
        try:
            results = self.client.retrieve(
                collection_name=self.config.collection_name,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
            return results[0] if results else None
        except UnexpectedResponse as e:
            logger.error(f"Failed to get point {point_id}: {e}")
            return None

    def list_sessions(
        self,
        repo_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        List indexed sessions.

        Args:
            repo_id: Optional repo ID to filter by
            limit: Maximum number of sessions to return

        Returns:
            List of session metadata dicts
        """
        filter_conditions = [
            models.FieldCondition(
                key="type",
                match=models.MatchValue(value="intent"),
            )
        ]

        if repo_id:
            filter_conditions.append(
                models.FieldCondition(
                    key="repo_id",
                    match=models.MatchValue(value=repo_id),
                )
            )

        try:
            results, _ = self.client.scroll(
                collection_name=self.config.collection_name,
                scroll_filter=models.Filter(must=filter_conditions),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )

            sessions = []
            for record in results:
                payload = record.payload or {}
                sessions.append({
                    "session_id": payload.get("session_id"),
                    "repo_name": payload.get("repo_name"),
                    "repo_path": payload.get("repo_path"),
                    "machine": payload.get("machine"),
                    "timestamp": payload.get("timestamp"),
                    "chunk_count": payload.get("transcript_chunk_count", 0),
                })

            return sessions
        except UnexpectedResponse as e:
            logger.error(f"Failed to list sessions: {e}")
            raise

    def get_collection_info(self) -> Optional[dict[str, Any]]:
        """
        Get collection statistics.

        Returns:
            Dict with collection info or None if collection doesn't exist
        """
        try:
            info = self.client.get_collection(self.config.collection_name)
            return {
                "name": self.config.collection_name,
                "points_count": info.points_count,
                "indexed_vectors_count": info.indexed_vectors_count,
                "segments_count": info.segments_count,
                "status": info.status.value if info.status else "unknown",
            }
        except UnexpectedResponse:
            return None

    def health_check(self) -> bool:
        """
        Check if Qdrant is reachable and collection exists.

        Returns:
            True if healthy
        """
        try:
            info = self.get_collection_info()
            return info is not None
        except Exception:
            return False
