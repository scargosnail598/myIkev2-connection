package com.saeed.ikev2vpn.vpn

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VpnStateReducerTest {
    @Test
    fun `successful start request remains unconfirmed connecting`() {
        val state = VpnStateReducer.reduce(VpnState(), VpnStateEvent.ConnectRequested)

        assertEquals(ConnectionState.CONNECTING, state.connectionState)
        assertEquals(StateEvidence.LOCAL_REQUEST, state.evidence)
        assertFalse(state.confirmed)
    }

    @Test
    fun `owned VPN network confirms connection`() {
        val connecting = VpnStateReducer.reduce(VpnState(), VpnStateEvent.ConnectRequested)
        val connected = VpnStateReducer.reduce(connecting, VpnStateEvent.OwnedNetworkAvailable)

        assertEquals(ConnectionState.CONNECTED, connected.connectionState)
        assertEquals(StateEvidence.OWNED_VPN_NETWORK, connected.evidence)
        assertTrue(connected.confirmed)
    }

    @Test
    fun `disconnect waits until owned network is lost`() {
        val connected = VpnStateReducer.reduce(VpnState(), VpnStateEvent.OwnedNetworkAvailable)
        val disconnecting = VpnStateReducer.reduce(connected, VpnStateEvent.DisconnectRequested)
        val disconnected = VpnStateReducer.reduce(disconnecting, VpnStateEvent.OwnedNetworkLost)

        assertEquals(ConnectionState.DISCONNECTING, disconnecting.connectionState)
        assertFalse(disconnecting.confirmed)
        assertEquals(ConnectionState.DISCONNECTED, disconnected.connectionState)
        assertTrue(disconnected.confirmed)
    }

    @Test
    fun `local stop completion remains unconfirmed`() {
        val disconnected = VpnStateReducer.reduce(
            VpnStateReducer.reduce(VpnState(), VpnStateEvent.DisconnectRequested),
            VpnStateEvent.LocalDisconnectCompleted,
        )

        assertEquals(ConnectionState.DISCONNECTED, disconnected.connectionState)
        assertEquals(StateEvidence.LOCAL_REQUEST, disconnected.evidence)
        assertFalse(disconnected.confirmed)
    }

    @Test
    fun `unconfirmed legacy disconnect timeout becomes unknown`() {
        val disconnecting = VpnStateReducer.reduce(
            VpnState(connectionState = ConnectionState.CONNECTED, confirmed = true),
            VpnStateEvent.DisconnectRequested,
        )
        val unknown = VpnStateReducer.reduce(
            disconnecting,
            VpnStateEvent.ConfirmationTimedOut(
                "Android did not confirm that the VPN network was removed.",
                StateEvidence.LOCAL_REQUEST,
            ),
        )

        assertEquals(ConnectionState.UNKNOWN, unknown.connectionState)
        assertEquals(StateEvidence.LOCAL_REQUEST, unknown.evidence)
        assertFalse(unknown.confirmed)
    }

    @Test
    fun `stale API state does not regress a requested transition`() {
        assertFalse(
            VpnStateReducer.shouldApplyPlatformObservation(
                ConnectionState.CONNECTING,
                ConnectionState.DISCONNECTED,
            ),
        )
        assertFalse(
            VpnStateReducer.shouldApplyPlatformObservation(
                ConnectionState.DISCONNECTING,
                ConnectionState.CONNECTED,
            ),
        )
        assertTrue(
            VpnStateReducer.shouldApplyPlatformObservation(
                ConnectionState.CONNECTING,
                ConnectionState.CONNECTED,
            ),
        )
        assertTrue(
            VpnStateReducer.shouldApplyPlatformObservation(
                ConnectionState.DISCONNECTING,
                ConnectionState.DISCONNECTED,
            ),
        )
    }

    @Test
    fun `recoverable platform error remains in retrying state`() {
        val recovering = VpnStateReducer.reduce(
            VpnState(connectionState = ConnectionState.CONNECTED),
            VpnStateEvent.Recovering("The carrying network was lost."),
        )

        assertEquals(ConnectionState.CONNECTING, recovering.connectionState)
        assertEquals(StateEvidence.API_33_EVENT, recovering.evidence)
        assertTrue(recovering.confirmed)
        assertEquals(null, recovering.error)
    }

    @Test
    fun `API 33 platform state is authoritative`() {
        val state = VpnStateReducer.reduce(
            VpnState(),
            VpnStateEvent.PlatformStateObserved(ConnectionState.CONNECTED, "session-123"),
        )

        assertEquals(ConnectionState.CONNECTED, state.connectionState)
        assertEquals(StateEvidence.API_33_PROFILE_STATE, state.evidence)
        assertEquals("session-123", state.sessionId)
        assertTrue(state.confirmed)
    }

    @Test
    fun `unconfirmed failure transitions to error without claiming confirmation`() {
        val state = VpnStateReducer.reduce(
            VpnStateReducer.reduce(VpnState(), VpnStateEvent.ConnectRequested),
            VpnStateEvent.Failed("Connection was not confirmed.", StateEvidence.LOCAL_REQUEST, false),
        )

        assertEquals(ConnectionState.ERROR, state.connectionState)
        assertFalse(state.confirmed)
    }

    @Test
    fun `API 33 failed state has a useful fallback error`() {
        val state = VpnStateReducer.reduce(
            VpnState(),
            VpnStateEvent.PlatformStateObserved(ConnectionState.ERROR),
        )

        assertEquals(ConnectionState.ERROR, state.connectionState)
        assertTrue(state.error.orEmpty().contains("failed"))
    }

    @Test
    fun `unconfirmed platform timeout requires a stop before retry`() {
        val state = VpnStateReducer.reduce(
            VpnStateReducer.reduce(VpnState(), VpnStateEvent.ConnectRequested),
            VpnStateEvent.ConfirmationTimedOut(
                "Android did not confirm the request.",
                StateEvidence.API_33_PROFILE_STATE,
            ),
        )

        assertEquals(ConnectionState.UNKNOWN, state.connectionState)
        assertFalse(state.confirmed)
        assertEquals("Android did not confirm the request.", state.error)
    }
}
