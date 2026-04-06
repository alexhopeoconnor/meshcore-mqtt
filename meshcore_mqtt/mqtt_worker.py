"""Independent MQTT worker with inbox/outbox message handling."""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt

from .auth_token import create_auth_token
from .config import Config, ConnectionType, MQTTAuthMethod, MQTTTransport
from .message_queue import (
    ComponentStatus,
    Message,
    MessageBus,
    MessageQueue,
    MessageType,
    get_message_bus,
)
from .serial_auth import read_device_keys


class MQTTWorker:
    """Independent MQTT worker managing broker connection and messaging."""

    def __init__(self, config: Config) -> None:
        """Initialize MQTT worker."""
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Component identification
        self.component_name = "mqtt"

        # Message bus
        self.message_bus: MessageBus = get_message_bus()
        self.inbox: MessageQueue = self.message_bus.register_component(
            self.component_name, queue_size=1000
        )

        # MQTT client
        self.client: Optional[mqtt.Client] = None
        self._token_cache: Optional[Tuple[str, float]] = None
        self._token_public_key: Optional[str] = None
        self._token_private_key: Optional[str] = None

        # Connection state
        self._connected = False
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._last_activity: Optional[float] = None

        # Worker state
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """Start the MQTT worker."""
        if self._running:
            self.logger.warning("MQTT worker is already running")
            return

        self.logger.info("Starting MQTT worker")
        self._running = True

        # Capture the current event loop for use in callbacks
        self._event_loop = asyncio.get_running_loop()

        # Update status
        self.message_bus.update_component_status(
            self.component_name, ComponentStatus.STARTING
        )

        try:
            # Setup MQTT connection
            await self._setup_connection()

            # Start worker tasks
            tasks = [
                asyncio.create_task(self._message_processor(), name="mqtt_processor"),
                asyncio.create_task(self._health_monitor(), name="mqtt_health"),
            ]
            self._tasks.extend(tasks)

            # Update status to running
            self.message_bus.update_component_status(
                self.component_name, ComponentStatus.RUNNING
            )

            self.logger.info("MQTT worker started successfully")

            # Wait for shutdown
            await self._shutdown_event.wait()

        except Exception as e:
            self.logger.error(f"Error starting MQTT worker: {e}")
            self.message_bus.update_component_status(
                self.component_name, ComponentStatus.ERROR
            )
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the MQTT worker."""
        if not self._running:
            return

        self.logger.info("Stopping MQTT worker")
        self.message_bus.update_component_status(
            self.component_name, ComponentStatus.STOPPING
        )

        self._running = False
        self._shutdown_event.set()

        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Stop MQTT connection
        if self.client:
            try:
                if hasattr(self.client, "_loop_started"):
                    self.client.loop_stop()
                    delattr(self.client, "_loop_started")

                if self.client.is_connected():
                    self.client.disconnect()
            except Exception as e:
                self.logger.error(f"Error stopping MQTT client: {e}")

        self.message_bus.update_component_status(
            self.component_name, ComponentStatus.STOPPED
        )
        self.logger.info("MQTT worker stopped")

    async def _setup_connection(self) -> None:
        """Set up MQTT connection."""
        self.logger.info("Setting up MQTT connection")

        # Create and configure client
        self.client = self._create_client()

        # Connect with retry logic
        try:
            await self._connect_with_retry()
            # Note: Don't set _connected=True here - wait for _on_connect callback
            # Note: Don't send CONNECTED status here - wait for _on_connect callback

        except Exception as e:
            await self._send_status_update(
                ComponentStatus.ERROR, f"connection_failed: {e}"
            )
            raise RuntimeError(f"Failed to connect to MQTT broker: {e}")

        # Start client loop
        if self.client:
            self.client.loop_start()

    def _create_client(self) -> mqtt.Client:
        """Create and configure a new MQTT client."""
        # Generate a unique client ID
        client_id = f"meshcore-mqtt-{uuid.uuid4().hex[:8]}"
        self.logger.debug(f"Using MQTT client ID: {client_id}")

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            reconnect_on_failure=True,
            transport=self.config.mqtt.transport.value,
        )

        # Set up callbacks
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect  # type: ignore
        client.on_message = self._on_message
        client.on_publish = self._on_publish
        client.on_log = self._on_log

        # Set authentication if configured
        username, password = self._resolve_auth_credentials()
        if username is not None:
            client.username_pw_set(username, password)

        # Configure TLS if enabled
        if self.config.mqtt.tls_enabled:
            self._configure_tls(client)

        if self.config.mqtt.transport == MQTTTransport.WEBSOCKETS:
            client.ws_set_options(path=self.config.mqtt.ws_path)
            self.logger.info(
                f"Configuring MQTT WebSockets transport on path {self.config.mqtt.ws_path}"
            )

        # Set connection parameters
        client.keepalive = 60
        client.max_inflight_messages_set(1)
        client.max_queued_messages_set(100)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        return client

    def _resolve_auth_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolve MQTT credentials, including MeshCore token auth."""
        auth_method = self.config.mqtt.resolved_auth_method()

        if auth_method == MQTTAuthMethod.NONE:
            return None, None

        if auth_method == MQTTAuthMethod.PASSWORD:
            if not self.config.mqtt.username:
                raise RuntimeError("MQTT username is required for password auth")
            return self.config.mqtt.username, self.config.mqtt.password

        public_key, private_key = self._resolve_token_keypair()
        token = self._get_or_create_auth_token(public_key, private_key)
        username = self.config.mqtt.username or f"v1_{public_key}"
        return username, token

    def _resolve_token_keypair(self) -> Tuple[str, str]:
        """Resolve MeshCore key material for token auth."""
        if self._token_public_key and self._token_private_key:
            return self._token_public_key, self._token_private_key

        if self.config.mqtt.token_public_key and self.config.mqtt.token_private_key:
            self._token_public_key = self.config.mqtt.token_public_key
            self._token_private_key = self.config.mqtt.token_private_key
            return self._token_public_key, self._token_private_key

        if self.config.meshcore.connection_type != ConnectionType.SERIAL:
            raise RuntimeError(
                "MQTT token auth requires a serial MeshCore connection or explicit "
                "MQTT_TOKEN_PUBLIC_KEY / MQTT_TOKEN_PRIVATE_KEY overrides"
            )

        self.logger.info(
            f"Reading MeshCore key material from serial device {self.config.meshcore.address}"
        )
        public_key, private_key = read_device_keys(
            self.config.meshcore.address,
            baudrate=self.config.meshcore.baudrate,
            timeout=float(self.config.meshcore.timeout),
        )
        self._token_public_key = public_key
        self._token_private_key = private_key
        return public_key, private_key

    def _get_or_create_auth_token(self, public_key: str, private_key: str) -> str:
        """Return a cached auth token or generate a fresh one."""
        now = time.time()
        expiry = self.config.mqtt.token_expiry_seconds
        refresh_window = min(300, max(expiry // 10, 30))

        if self._token_cache is not None:
            cached_token, created_at = self._token_cache
            if now - created_at < max(expiry - refresh_window, 1):
                return cached_token

        claims: Dict[str, Any] = {"client": "meshcore-mqtt"}
        if self.config.mqtt.token_audience:
            claims["aud"] = self.config.mqtt.token_audience

        if self.config.mqtt.tls_enabled and not self.config.mqtt.tls_insecure:
            if self.config.mqtt.token_owner:
                claims["owner"] = self.config.mqtt.token_owner
            if self.config.mqtt.token_email:
                claims["email"] = self.config.mqtt.token_email.lower()

        token = create_auth_token(
            public_key,
            private_key,
            expiry_seconds=expiry,
            **claims,
        )
        self._token_cache = (token, now)
        self.logger.info("Generated MQTT auth token from MeshCore device key material")
        return token

    def _configure_tls(self, client: mqtt.Client) -> None:
        """Configure TLS settings for MQTT client."""
        self.logger.info("Configuring MQTT TLS connection")

        try:
            if (
                not self.config.mqtt.tls_ca_cert
                and not self.config.mqtt.tls_client_cert
                and not self.config.mqtt.tls_client_key
            ):
                # Simple TLS setup (e.g., Let's Encrypt)
                client.tls_set()
                self.logger.info("Using default TLS configuration")
            else:
                # Custom certificate setup
                client.tls_set(
                    ca_certs=self.config.mqtt.tls_ca_cert,
                    certfile=self.config.mqtt.tls_client_cert,
                    keyfile=self.config.mqtt.tls_client_key,
                )
                self.logger.info("Using custom TLS certificates")

            # Handle insecure mode
            if self.config.mqtt.tls_insecure:
                self.logger.warning("TLS certificate verification disabled")
                client.tls_insecure_set(True)

            self.logger.info("MQTT TLS configuration completed successfully")

        except Exception as e:
            self.logger.error(f"Failed to configure MQTT TLS: {e}")
            raise RuntimeError(f"TLS configuration failed: {e}")

    async def _connect_with_retry(self, max_retries: int = 5) -> None:
        """Connect to MQTT broker with retry logic."""
        for attempt in range(max_retries):
            try:
                if self.client:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: self.client.connect(  # type: ignore
                            self.config.mqtt.broker, self.config.mqtt.port, 60
                        ),
                    )
                    self.logger.info(
                        f"MQTT connection initiated on attempt {attempt + 1}"
                    )
                    return
            except Exception as e:
                self.logger.warning(
                    f"MQTT connection attempt {attempt + 1} failed: {e}"
                )
                if attempt < max_retries - 1:
                    delay = min(2**attempt, 30)
                    self.logger.info(f"Retrying MQTT connection in {delay} seconds")
                    await asyncio.sleep(delay)
                else:
                    raise RuntimeError(
                        f"Failed to connect to MQTT broker after {max_retries} "
                        f"attempts: {e}"
                    )

    async def _message_processor(self) -> None:
        """Process messages from the inbox."""
        self.logger.info("Starting MQTT message processor")

        while self._running:
            try:
                # Get message from inbox with timeout
                message = await self.inbox.get(timeout=1.0)
                if message is None:
                    continue

                await self._handle_inbox_message(message)

            except Exception as e:
                self.logger.error(f"Error in message processor: {e}")
                await asyncio.sleep(1)

    async def _handle_inbox_message(self, message: Message) -> None:
        """Handle a message from the inbox."""
        self.logger.debug(f"Processing message: {message.message_type.value}")

        try:
            if message.message_type == MessageType.MESHCORE_EVENT:
                await self._handle_meshcore_event(message)
            elif message.message_type == MessageType.MESHCORE_STATUS:
                await self._handle_meshcore_status(message)
            elif message.message_type == MessageType.HEALTH_CHECK:
                await self._handle_health_check(message)
            elif message.message_type == MessageType.SHUTDOWN:
                self.logger.info("Received shutdown message")
                self._shutdown_event.set()
            else:
                self.logger.warning(
                    f"Unknown message type: {message.message_type.value}"
                )

        except Exception as e:
            self.logger.error(f"Error handling message {message.id}: {e}")

    async def _handle_meshcore_event(self, message: Message) -> None:
        """Handle MeshCore event and publish to MQTT."""
        # Check if MQTT is connected before processing
        if not self._connected:
            self.logger.debug("MQTT not connected, queuing event for later")
            # Could implement a retry queue here if needed
            return

        event_payload = message.payload
        event_data = event_payload.get("event_data")

        if not event_data:
            self.logger.warning("Received empty MeshCore event data")
            return

        try:
            # Determine message type and create appropriate topic structure
            topic = self._determine_mqtt_topic(event_data)
            payload = self._serialize_to_json(event_data)

            # Publish to MQTT
            self.logger.debug(f"Publishing MeshCore event to MQTT topic: {topic}")
            success = await self._safe_mqtt_publish(topic, payload)

            if success:
                self.logger.debug(f"Published MeshCore event to MQTT: {topic}")
            else:
                self.logger.warning(
                    f"Failed to publish MeshCore event to MQTT: {topic}"
                )

        except Exception as e:
            self.logger.error(f"Error processing MeshCore event: {e}")

    def _determine_mqtt_topic(self, event_data: Any) -> str:
        """Determine the appropriate MQTT topic for the event data."""
        try:
            # Check if this is a message event
            if hasattr(event_data, "payload") and isinstance(event_data.payload, dict):
                message_data = event_data.payload
                message_type = message_data.get("type", "")

                if message_type == "CHAN":
                    # Channel message - use channel identifier
                    channel_idx = message_data.get("channel_idx", 0)
                    return (
                        f"{self.config.mqtt.topic_prefix}/message/channel/{channel_idx}"
                    )
                elif message_type == "PRIV":
                    # Direct message - use sender's public key prefix
                    pubkey_prefix = message_data.get("pubkey_prefix", "unknown")
                    return (
                        f"{self.config.mqtt.topic_prefix}/message/direct/"
                        f"{pubkey_prefix}"
                    )

            # Check event type for non-message events
            event_type_name = getattr(event_data, "type", None)
            if event_type_name:
                event_name = str(event_type_name).split(".")[-1]  # Get enum name

                if event_name in ["CONNECTED", "DISCONNECTED"]:
                    return f"{self.config.mqtt.topic_prefix}/events/connection"
                elif event_name in ["LOGIN_SUCCESS", "LOGIN_FAILED"]:
                    return f"{self.config.mqtt.topic_prefix}/login"
                elif event_name == "DEVICE_INFO":
                    return f"{self.config.mqtt.topic_prefix}/device_info"
                elif event_name == "BATTERY":
                    return f"{self.config.mqtt.topic_prefix}/battery"
                elif event_name == "NEW_CONTACT":
                    return f"{self.config.mqtt.topic_prefix}/new_contact"
                elif event_name == "ADVERTISEMENT":
                    return f"{self.config.mqtt.topic_prefix}/advertisement"
                elif event_name == "TRACE_DATA":
                    # Extract tag from trace data for topic path
                    trace_tag = "unknown"
                    if hasattr(event_data, "payload") and event_data.payload:
                        trace_tag = event_data.payload.get("tag", "unknown")
                    elif hasattr(event_data, "attributes") and event_data.attributes:
                        trace_tag = event_data.attributes.get("tag", "unknown")
                    return f"{self.config.mqtt.topic_prefix}/traceroute/{trace_tag}"
                elif event_name == "TELEMETRY_RESPONSE":
                    return f"{self.config.mqtt.topic_prefix}/telemetry"
                elif event_name == "CONTACTS":
                    return f"{self.config.mqtt.topic_prefix}/contacts"
                elif event_name == "SELF_INFO":
                    return f"{self.config.mqtt.topic_prefix}/self_info"
                elif event_name == "CHANNEL_INFO":
                    return f"{self.config.mqtt.topic_prefix}/channel_info"

            # Fallback for unknown event types
            return f"{self.config.mqtt.topic_prefix}/event"

        except Exception as e:
            self.logger.warning(f"Error determining MQTT topic: {e}")
            return f"{self.config.mqtt.topic_prefix}/event"

    async def _handle_meshcore_status(self, message: Message) -> None:
        """Handle MeshCore status update."""
        status_payload = message.payload
        status = status_payload.get("status")
        details = status_payload.get("details", "")

        self.logger.info(f"MeshCore status update: {status} - {details}")

        # Only publish to MQTT if we're connected
        if not self._connected:
            self.logger.debug("MQTT not connected, skipping status publish")
            return

        # Publish status to MQTT
        topic = f"{self.config.mqtt.topic_prefix}/status"

        if status == "connected":
            payload = "connected"
        elif status == "disconnected":
            payload = "disconnected"
        elif status == "error":
            payload = f"error: {details}"
        else:
            payload = f"{status}: {details}"

        await self._safe_mqtt_publish(topic, payload, retain=True)

    async def _handle_health_check(self, message: Message) -> None:
        """Handle health check request."""
        healthy = self._is_healthy()

        # Send health status back
        response = Message.create(
            message_type=MessageType.HEALTH_CHECK,
            source=self.component_name,
            target=message.source,
            payload={
                "healthy": healthy,
                "connected": self._connected,
                "last_activity": self._last_activity,
            },
        )
        await self.message_bus.send_message(response)

    async def _health_monitor(self) -> None:
        """Monitor MQTT connection health."""
        self.logger.info("Starting MQTT health monitor")

        # Wait for initial connection to stabilize
        await asyncio.sleep(5)

        while self._running:
            try:
                healthy = self._is_healthy()

                if not healthy and self._connected:
                    self.logger.warning("MQTT health check failed, attempting recovery")
                    await self._recover_connection()

                await asyncio.sleep(10)  # Health check every 10 seconds

            except Exception as e:
                self.logger.error(f"Error in health monitor: {e}")
                await asyncio.sleep(30)

    def _is_healthy(self) -> bool:
        """Check if MQTT connection is healthy."""
        if not self.client:
            return False

        # Check basic connectivity
        if not self.client.is_connected():
            return False

        # Check for stale connections
        if self._is_stale():
            return False

        return True

    def _is_stale(self, timeout_seconds: int = 300) -> bool:
        """Check if connection appears stale."""
        if not self._last_activity:
            return False
        return time.time() - self._last_activity > timeout_seconds

    async def _recover_connection(self) -> None:
        """Recover MQTT connection with complete client recreation."""
        if self._reconnecting:
            self.logger.debug("MQTT recovery already in progress")
            return

        if self._reconnect_attempts >= self._max_reconnect_attempts:
            self.logger.error("Max MQTT reconnection attempts reached")
            await self._send_status_update(
                ComponentStatus.ERROR, "max_reconnect_attempts"
            )
            return

        self._reconnecting = True
        self._reconnect_attempts += 1

        self.logger.warning(
            f"Starting MQTT recovery (attempt "
            f"{self._reconnect_attempts}/{self._max_reconnect_attempts})"
        )

        # Update status
        await self._send_status_update(ComponentStatus.DISCONNECTED, "reconnecting")

        try:
            # Destroy old client
            await self._destroy_client()

            # Wait with exponential backoff
            delay = min(2**self._reconnect_attempts, 30)
            self.logger.info(f"Waiting {delay}s before MQTT reconnection")
            await asyncio.sleep(delay)

            # Create fresh client and connection
            await self._create_fresh_connection()

            # Success - reset counters
            self._reconnect_attempts = 0
            self._reconnecting = False
            # Note: Don't set _connected=True here - wait for _on_connect callback
            # Note: Don't send CONNECTED status here - wait for _on_connect callback

            self.logger.info("MQTT connection recovery initiated")

        except Exception as e:
            self.logger.error(
                f"MQTT recovery attempt {self._reconnect_attempts} failed: {e}"
            )
            self._reconnecting = False
            await self._send_status_update(
                ComponentStatus.ERROR, f"recovery_failed: {e}"
            )

            # Schedule retry if we haven't hit max attempts
            if self._reconnect_attempts < self._max_reconnect_attempts:
                retry_delay = min(5 * self._reconnect_attempts, 60)
                self.logger.info(f"Scheduling MQTT retry in {retry_delay}s")
                await asyncio.sleep(retry_delay)
                if self._running:
                    asyncio.create_task(self._recover_connection())
            else:
                self.logger.error("🚨 MQTT recovery failed permanently")

    async def _destroy_client(self) -> None:
        """Destroy the existing MQTT client."""
        if not self.client:
            return

        self.logger.debug("Destroying old MQTT client")

        try:
            if hasattr(self.client, "_loop_started"):
                self.client.loop_stop()
                delattr(self.client, "_loop_started")

            if self.client.is_connected():
                self.client.disconnect()

            # Remove callbacks
            self.client.on_connect = None
            self.client.on_disconnect = None
            self.client.on_message = None
            self.client.on_publish = None
            self.client.on_log = None

        except Exception as e:
            self.logger.debug(f"Error during MQTT client destruction: {e}")
        finally:
            self.client = None
            self._connected = False

    async def _create_fresh_connection(self) -> None:
        """Create fresh client and establish connection."""
        self.logger.info("Creating fresh MQTT client")

        # Create and configure new client
        self.client = self._create_client()

        # Connect
        self.logger.debug(
            f"Connecting to {self.config.mqtt.broker}:{self.config.mqtt.port}"
        )

        if self.client:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.connect(  # type: ignore
                    self.config.mqtt.broker, self.config.mqtt.port, 60
                ),
            )

        # Start client loop
        self.client.loop_start()

        # Wait for connection
        await asyncio.sleep(2)

        if not self.client.is_connected():
            raise RuntimeError("MQTT client failed to connect")

        self.logger.info("Fresh MQTT client connected")

    async def _safe_mqtt_publish(
        self, topic: str, payload: str, retain: bool = False
    ) -> bool:
        """Safely publish to MQTT broker."""
        if not self.client:
            self.logger.error("MQTT client not initialized")
            return False

        try:
            if not self.client.is_connected():
                self.logger.warning(
                    f"MQTT client not connected, skipping publish to {topic}"
                )
                if self._running:
                    asyncio.create_task(self._recover_connection())
                return False

            qos = self.config.mqtt.qos
            retain = retain or self.config.mqtt.retain

            self.logger.debug(
                f"Publishing to MQTT: topic={topic}, qos={qos}, retain={retain}, "
                f"payload_length={len(payload)}"
            )

            result = self.client.publish(topic, payload, qos=qos, retain=retain)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._last_activity = time.time()
                if qos > 0:
                    result.wait_for_publish(timeout=5.0)
                return True
            elif result.rc == mqtt.MQTT_ERR_NO_CONN:
                self.logger.warning(f"MQTT not connected while publishing to {topic}")
                if self._running:
                    asyncio.create_task(self._recover_connection())
                return False
            else:
                self.logger.error(
                    f"Failed to publish to MQTT topic {topic}: "
                    f"{mqtt.error_string(result.rc)} ({result.rc})"
                )
                return False

        except (ConnectionError, OSError, BrokenPipeError) as e:
            self.logger.error(f"Connection error during MQTT publish to {topic}: {e}")
            if self._running:
                asyncio.create_task(self._recover_connection())
            return False
        except Exception as e:
            self.logger.error(
                f"Unexpected exception during MQTT publish to {topic}: {e}"
            )
            return False

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Dict[str, Any],
        rc: int,
        properties: Any = None,
    ) -> None:
        """Handle MQTT connection."""
        if rc == 0:
            self.logger.info("Connected to MQTT broker")
            self._connected = True
            self._last_activity = time.time()

            # Update component status in message bus
            self.message_bus.update_component_status(
                self.component_name, ComponentStatus.CONNECTED
            )

            # Subscribe to command topics
            command_topic = f"{self.config.mqtt.topic_prefix}/command/+"
            client.subscribe(command_topic, self.config.mqtt.qos)
            self.logger.info(f"Subscribed to MQTT topic: {command_topic}")
        else:
            self.logger.error(f"Failed to connect to MQTT broker: {rc}")
            self._connected = False
            self.message_bus.update_component_status(
                self.component_name, ComponentStatus.ERROR
            )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Dict[str, Any],
        rc: int,
        properties: Any = None,
    ) -> None:
        """Handle MQTT disconnection."""
        self._connected = False

        # Update component status in message bus
        self.message_bus.update_component_status(
            self.component_name, ComponentStatus.DISCONNECTED
        )

        if rc != 0:
            self.logger.warning(
                f"🔴 Unexpected MQTT disconnection: {mqtt.error_string(rc)} (code: {rc})"
            )
            if self._running and not self._reconnecting:
                self.logger.info("Triggering MQTT recovery from disconnect callback")
                asyncio.create_task(self._recover_connection())
        else:
            self.logger.info("MQTT client disconnected cleanly")

    def _on_message(
        self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        """Handle incoming MQTT messages."""
        try:
            topic_parts = message.topic.split("/")
            if len(topic_parts) >= 3 and topic_parts[1] == "command":
                command_type = topic_parts[2]
                payload = message.payload.decode("utf-8")

                self.logger.info(f"Received MQTT command: {command_type} = {payload}")

                # Forward command to MeshCore worker
                self._forward_command_to_meshcore(command_type, payload)

        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}")

    def _forward_command_to_meshcore(self, command_type: str, payload: str) -> None:
        """Forward MQTT command to MeshCore worker."""
        try:
            # Parse command payload
            if payload.startswith("{"):
                command_data = json.loads(payload)
            else:
                command_data = {"data": payload}

            # Add command type to payload
            command_data["command_type"] = command_type

            # Create message for MeshCore worker
            message = Message.create(
                message_type=MessageType.MQTT_COMMAND,
                source=self.component_name,
                target="meshcore",
                payload=command_data,
            )

            # Send to message bus (non-blocking)
            # Use run_coroutine_threadsafe since called from MQTT callback thread
            if self._event_loop and not self._event_loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.message_bus.send_message(message), self._event_loop
                )
            else:
                self.logger.warning(
                    "No valid event loop available, dropping MQTT command"
                )

        except Exception as e:
            self.logger.error(f"Error forwarding MQTT command to MeshCore: {e}")

    def _on_publish(
        self,
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_codes: Any = None,
        properties: Any = None,
    ) -> None:
        """Handle MQTT publish confirmation."""
        self.logger.debug(f"MQTT message published: {mid}")

    def _on_log(self, client: mqtt.Client, userdata: Any, level: int, buf: str) -> None:
        """Handle MQTT logging."""
        if level == mqtt.MQTT_LOG_DEBUG:
            self.logger.debug(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_INFO:
            self.logger.info(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_NOTICE:
            self.logger.info(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_WARNING:
            self.logger.warning(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_ERR:
            self.logger.error(f"MQTT: {buf}")
        else:
            self.logger.debug(f"MQTT ({level}): {buf}")

    async def _send_status_update(self, status: ComponentStatus, details: str) -> None:
        """Send status update to other components."""
        self.message_bus.update_component_status(self.component_name, status)

        self.logger.info(f"MQTT status update: {status.value} - {details}")

    def _serialize_to_json(self, data: Any) -> str:
        """Safely serialize any data to JSON string."""
        from datetime import datetime, timezone

        try:
            # Handle common data types
            if isinstance(data, (dict, list, str, int, float, bool)) or data is None:
                return json.dumps(data, ensure_ascii=False)

            # Handle objects with custom serialization
            if hasattr(data, "__dict__"):
                obj_dict = {
                    key: value
                    for key, value in data.__dict__.items()
                    if not key.startswith("_")
                }
                if obj_dict:
                    return json.dumps(obj_dict, ensure_ascii=False, default=str)

            # Handle iterables
            if hasattr(data, "__iter__") and not isinstance(data, (str, bytes)):
                try:
                    return json.dumps(list(data), ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    pass

            # Fallback: structured JSON with metadata
            return json.dumps(
                {
                    "type": type(data).__name__,
                    "value": str(data),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )

        except Exception as e:
            self.logger.warning(f"Failed to serialize data to JSON: {e}")
            return json.dumps(
                {
                    "error": f"Serialization failed: {str(e)}",
                    "raw_value": str(data)[:1000],
                    "type": type(data).__name__,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
