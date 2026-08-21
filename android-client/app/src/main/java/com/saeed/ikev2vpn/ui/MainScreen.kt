package com.saeed.ikev2vpn.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.saeed.ikev2vpn.vpn.ConnectionState

@Composable
fun MainScreen(
    state: VpnUiState,
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onEdit: () -> Unit,
    onDiagnostics: () -> Unit,
    onDismissError: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("IKEv2 VPN", style = MaterialTheme.typography.headlineMedium)
        Spacer(Modifier.height(40.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(14.dp)
                    .background(stateColor(state.connectionState), CircleShape),
            )
            Text(
                text = "  ${state.connectionState.displayName()}",
                style = MaterialTheme.typography.titleLarge,
            )
        }
        Text(
            text = if (state.stateConfirmed) "Confirmed by Android" else "Awaiting platform confirmation",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(24.dp))

        val disconnecting = state.connectionState == ConnectionState.DISCONNECTING
        val connecting = state.connectionState == ConnectionState.CONNECTING
        val connected = state.connectionState == ConnectionState.CONNECTED
        val unknown = state.connectionState == ConnectionState.UNKNOWN
        Button(
            onClick = if (connected || connecting || unknown) onDisconnect else onConnect,
            modifier = Modifier
                .fillMaxWidth()
                .height(64.dp),
            enabled = !disconnecting && state.connectionState != ConnectionState.NOT_CONFIGURED,
        ) {
            Text(
                when {
                    connecting -> "CANCEL CONNECTION"
                    disconnecting -> "DISCONNECTING…"
                    connected -> "DISCONNECT"
                    unknown -> "STOP / RESET"
                    else -> "CONNECT"
                },
                style = MaterialTheme.typography.titleMedium,
            )
        }

        state.error?.let { error ->
            Spacer(Modifier.height(16.dp))
            ErrorCard(message = error, onDismiss = onDismissError)
        }

        Spacer(Modifier.height(28.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        ) {
            Column(
                modifier = Modifier.padding(18.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                SummaryRow("Profile", state.profileName)
                SummaryRow("Server", state.serverAddress)
                SummaryRow("User", state.username)
                SummaryRow("CA", state.certificateInfo?.displayName.orEmpty())
                SummaryRow("Connection", "IKEv2 / IPsec")
                SummaryRow("Authentication", "EAP-MSCHAPv2")
            }
        }

        Spacer(Modifier.weight(1f))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            OutlinedButton(
                onClick = onEdit,
                modifier = Modifier.weight(1f),
                enabled = state.connectionState in setOf(
                    ConnectionState.DISCONNECTED,
                    ConnectionState.ERROR,
                ),
            ) {
                Text("Edit Profile")
            }
            OutlinedButton(onClick = onDiagnostics, modifier = Modifier.weight(1f)) {
                Text("Diagnostics")
            }
        }
    }
}

@Composable
private fun SummaryRow(label: String, value: String) {
    Row(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = label,
            modifier = Modifier.weight(0.38f),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.labelLarge,
        )
        Text(text = value, modifier = Modifier.weight(0.62f))
    }
}

private fun stateColor(state: ConnectionState): Color = when (state) {
    ConnectionState.CONNECTED -> Color(0xFF16803C)
    ConnectionState.CONNECTING, ConnectionState.DISCONNECTING -> Color(0xFFE69B00)
    ConnectionState.ERROR -> Color(0xFFBA1A1A)
    else -> Color(0xFF6B7280)
}

internal fun ConnectionState.displayName(): String = when (this) {
    ConnectionState.NOT_CONFIGURED -> "Not Configured"
    ConnectionState.DISCONNECTED -> "Disconnected"
    ConnectionState.CONNECTING -> "Connecting"
    ConnectionState.CONNECTED -> "Connected"
    ConnectionState.DISCONNECTING -> "Disconnecting"
    ConnectionState.ERROR -> "Error"
    ConnectionState.UNKNOWN -> "Unknown"
}
