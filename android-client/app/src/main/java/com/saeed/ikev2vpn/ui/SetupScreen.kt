package com.saeed.ikev2vpn.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.saeed.ikev2vpn.validation.ProfileField

@Composable
fun SetupScreen(
    state: VpnUiState,
    onProfileNameChanged: (String) -> Unit,
    onServerChanged: (String) -> Unit,
    onUsernameChanged: (String) -> Unit,
    onImportIkevProfile: () -> Unit,
    onImportCertificate: () -> Unit,
    onProvision: (String) -> Unit,
    onBack: () -> Unit,
    onDismissError: () -> Unit,
) {
    var password by remember { mutableStateOf("") }
    LaunchedEffect(state.importedProfileInfo?.importRevision) {
        if (state.importedProfileInfo != null) password = ""
    }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) password = ""
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            password = ""
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = if (state.configured) "Edit VPN Profile" else "Set Up IKEv2 VPN",
                    style = MaterialTheme.typography.headlineMedium,
                )
                Text(
                    text = "Android provides the IKEv2/IPsec connection.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (state.configured) {
                TextButton(onClick = onBack, enabled = !state.isBusy) { Text("Cancel") }
            }
        }

        if (!state.platformSupported) {
            MessageCard(
                message = "This device does not support Android platform IKEv2 VPN profiles.",
                isError = true,
            )
        }
        state.error?.let { error ->
            ErrorCard(message = error, onDismiss = onDismissError)
        }

        Button(
            onClick = onImportIkevProfile,
            modifier = Modifier.fillMaxWidth(),
            enabled = !state.isBusy,
        ) {
            Text("Import .ikev Profile")
        }
        Text(
            text = "or configure manually below",
            modifier = Modifier.align(Alignment.CenterHorizontally),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        state.importedProfileInfo?.let { imported ->
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.secondaryContainer,
                ),
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Text("Imported .ikev v1", style = MaterialTheme.typography.titleMedium)
                    ImportedProfileRow("Server", state.serverAddress)
                    ImportedProfileRow("Remote ID", imported.remoteId)
                    ImportedProfileRow("Username", state.username)
                    ImportedProfileRow("Mode", "Full tunnel")
                    ImportedProfileRow(
                        "CA SHA-256",
                        state.certificateInfo?.sha256Fingerprint.orEmpty(),
                    )
                    ImportedProfileRow("Server profile", imported.serverProfile)
                    ImportedProfileRow("Proxy", imported.proxySummary)
                }
            }
        }

        OutlinedTextField(
            value = state.profileName,
            onValueChange = onProfileNameChanged,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Profile Name") },
            singleLine = true,
            isError = ProfileField.PROFILE_NAME in state.fieldErrors,
            supportingText = state.fieldErrors[ProfileField.PROFILE_NAME]?.let { message ->
                { Text(message) }
            },
        )
        OutlinedTextField(
            value = state.serverAddress,
            onValueChange = onServerChanged,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Server Address") },
            placeholder = { Text("vpn.example.com") },
            singleLine = true,
            isError = ProfileField.SERVER_ADDRESS in state.fieldErrors,
            supportingText = state.fieldErrors[ProfileField.SERVER_ADDRESS]?.let { message ->
                { Text(message) }
            },
        )
        OutlinedTextField(
            value = state.username,
            onValueChange = onUsernameChanged,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Username") },
            singleLine = true,
            isError = ProfileField.USERNAME in state.fieldErrors,
            supportingText = state.fieldErrors[ProfileField.USERNAME]?.let { message ->
                { Text(message) }
            },
        )
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Password") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            isError = ProfileField.PASSWORD in state.fieldErrors,
            supportingText = {
                Text(
                    state.fieldErrors[ProfileField.PASSWORD]
                        ?: "Required when provisioning; never stored by this app.",
                )
            },
        )

        Text(text = "CA Certificate", style = MaterialTheme.typography.titleMedium)
        state.certificateInfo?.let { certificate ->
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceContainer,
                ),
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Text("✓ ${certificate.displayName}", style = MaterialTheme.typography.titleSmall)
                    Text("Subject", style = MaterialTheme.typography.labelMedium)
                    Text(certificate.subject, style = MaterialTheme.typography.bodySmall)
                    Text("Issuer", style = MaterialTheme.typography.labelMedium)
                    Text(certificate.issuer, style = MaterialTheme.typography.bodySmall)
                    Text("SHA-256", style = MaterialTheme.typography.labelMedium)
                    Text(
                        certificate.sha256Fingerprint,
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (!certificate.isCertificateAuthority) {
                        Text(
                            "Warning: this certificate is not marked as a CA certificate.",
                            color = MaterialTheme.colorScheme.error,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
        state.fieldErrors[ProfileField.CERTIFICATE]?.let { message ->
            Text(message, color = MaterialTheme.colorScheme.error)
        }
        OutlinedButton(
            onClick = onImportCertificate,
            modifier = Modifier.fillMaxWidth(),
            enabled = !state.isBusy,
        ) {
            Text(if (state.certificateInfo == null) "Import CA Certificate" else "Replace CA Certificate")
        }

        Spacer(Modifier.height(4.dp))
        Button(
            onClick = {
                val passwordForProvisioning = password
                password = ""
                onProvision(passwordForProvisioning)
            },
            modifier = Modifier
                .fillMaxWidth()
                .height(56.dp),
            enabled = state.platformSupported && !state.isBusy,
        ) {
            if (state.isBusy) {
                CircularProgressIndicator(
                    modifier = Modifier.height(22.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.onPrimary,
                )
            } else {
                Text("Save / Provision VPN")
            }
        }
    }
}

@Composable
private fun ImportedProfileRow(label: String, value: String) {
    Text(label, style = MaterialTheme.typography.labelMedium)
    Text(value, style = MaterialTheme.typography.bodySmall)
}

@Composable
internal fun ErrorCard(message: String, onDismiss: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer),
    ) {
        Row(
            modifier = Modifier.padding(start = 16.dp, top = 10.dp, bottom = 10.dp, end = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = message,
                modifier = Modifier.weight(1f),
                color = MaterialTheme.colorScheme.onErrorContainer,
            )
            TextButton(onClick = onDismiss) { Text("Dismiss") }
        }
    }
}

@Composable
private fun MessageCard(message: String, isError: Boolean) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (isError) {
                MaterialTheme.colorScheme.errorContainer
            } else {
                MaterialTheme.colorScheme.surfaceContainer
            },
        ),
    ) {
        Text(message, modifier = Modifier.padding(16.dp))
    }
}
