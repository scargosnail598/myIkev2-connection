package com.saeed.ikev2vpn.vpn

enum class ConnectionState {
    NOT_CONFIGURED,
    DISCONNECTED,
    CONNECTING,
    CONNECTED,
    DISCONNECTING,
    ERROR,
    UNKNOWN,
}

enum class StateEvidence {
    NONE,
    LOCAL_REQUEST,
    API_33_PROFILE_STATE,
    API_33_EVENT,
    OWNED_VPN_NETWORK,
}

data class VpnState(
    val connectionState: ConnectionState = ConnectionState.DISCONNECTED,
    val evidence: StateEvidence = StateEvidence.NONE,
    val confirmed: Boolean = false,
    val detail: String = "No active VPN session has been confirmed.",
    val sessionId: String? = null,
    val error: String? = null,
)

sealed interface VpnStateEvent {
    data object ConnectRequested : VpnStateEvent
    data object DisconnectRequested : VpnStateEvent
    data object LocalDisconnectCompleted : VpnStateEvent
    data class Recovering(val userMessage: String) : VpnStateEvent
    data class ConfirmationTimedOut(
        val userMessage: String,
        val evidence: StateEvidence,
    ) : VpnStateEvent
    data object OwnedNetworkAvailable : VpnStateEvent
    data object OwnedNetworkLost : VpnStateEvent
    data class PlatformStateObserved(
        val state: ConnectionState,
        val sessionId: String? = null,
    ) : VpnStateEvent
    data class Failed(
        val userMessage: String,
        val evidence: StateEvidence,
        val confirmed: Boolean,
    ) : VpnStateEvent
}

object VpnStateReducer {
    fun reduce(current: VpnState, event: VpnStateEvent): VpnState = when (event) {
        VpnStateEvent.ConnectRequested -> VpnState(
            connectionState = ConnectionState.CONNECTING,
            evidence = StateEvidence.LOCAL_REQUEST,
            detail = "Android accepted the connection request; tunnel establishment is not confirmed yet.",
        )
        VpnStateEvent.DisconnectRequested -> current.copy(
            connectionState = ConnectionState.DISCONNECTING,
            evidence = StateEvidence.LOCAL_REQUEST,
            confirmed = false,
            detail = "Waiting for Android to tear down the VPN session.",
            error = null,
        )
        VpnStateEvent.LocalDisconnectCompleted -> VpnState(
            connectionState = ConnectionState.DISCONNECTED,
            evidence = StateEvidence.LOCAL_REQUEST,
            confirmed = false,
            detail = "Android accepted the stop request; no owned VPN network was available to confirm teardown.",
        )
        is VpnStateEvent.Recovering -> current.copy(
            connectionState = ConnectionState.CONNECTING,
            evidence = StateEvidence.API_33_EVENT,
            confirmed = true,
            detail = "${event.userMessage} Android will retry automatically.",
            error = null,
        )
        is VpnStateEvent.ConfirmationTimedOut -> current.copy(
            connectionState = ConnectionState.UNKNOWN,
            evidence = event.evidence,
            confirmed = false,
            detail = event.userMessage,
            error = event.userMessage,
        )
        VpnStateEvent.OwnedNetworkAvailable -> current.copy(
            connectionState = ConnectionState.CONNECTED,
            evidence = StateEvidence.OWNED_VPN_NETWORK,
            confirmed = true,
            detail = "Android reports a VPN network owned by this application.",
            error = null,
        )
        VpnStateEvent.OwnedNetworkLost -> VpnState(
            connectionState = ConnectionState.DISCONNECTED,
            evidence = StateEvidence.OWNED_VPN_NETWORK,
            confirmed = true,
            detail = "No VPN network owned by this application is active.",
        )
        is VpnStateEvent.PlatformStateObserved -> {
            val platformError = if (event.state == ConnectionState.ERROR) {
                current.error ?: "Android reports that the VPN session failed."
            } else {
                null
            }
            VpnState(
                connectionState = event.state,
                evidence = StateEvidence.API_33_PROFILE_STATE,
                confirmed = true,
                detail = platformError ?: "State reported by Android's provisioned VPN profile API.",
                sessionId = event.sessionId,
                error = platformError,
            )
        }
        is VpnStateEvent.Failed -> current.copy(
            connectionState = ConnectionState.ERROR,
            evidence = event.evidence,
            confirmed = event.confirmed,
            detail = event.userMessage,
            error = event.userMessage,
        )
    }

    fun shouldApplyPlatformObservation(
        current: ConnectionState,
        observed: ConnectionState,
    ): Boolean = when (current) {
        ConnectionState.CONNECTING -> observed in setOf(
            ConnectionState.CONNECTING,
            ConnectionState.CONNECTED,
            ConnectionState.ERROR,
        )
        ConnectionState.DISCONNECTING -> observed in setOf(
            ConnectionState.DISCONNECTED,
            ConnectionState.ERROR,
        )
        else -> true
    }
}
