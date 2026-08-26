package com.saeed.ikev2vpn.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
import androidx.compose.material3.HorizontalDivider
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
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
    val hasDraftProfile = state.profileName.isNotBlank() ||
        state.serverAddress.isNotBlank() ||
        state.username.isNotBlank() ||
        state.certificateInfo != null
    var manualSetupExpanded by rememberSaveable {
        mutableStateOf(
            state.configured || (state.importedProfileInfo == null && hasDraftProfile),
        )
    }
    LaunchedEffect(state.importedProfileInfo?.importRevision) {
        if (state.importedProfileInfo != null) {
            password = ""
            manualSetupExpanded = false
        }
    }
    LaunchedEffect(state.fieldErrors) {
        if (state.fieldErrors.keys.any { it in MANUAL_PROFILE_FIELDS }) {
            manualSetupExpanded = true
        }
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
        verticalArrangement = Arrangement.spacedBy(20.dp),
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
                    text = "Import a profile, enter your password, and let Android handle the connection.",
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
            modifier = Modifier
                .fillMaxWidth()
                .height(52.dp),
            enabled = !state.isBusy,
        ) {
            Text("Import .ikev Profile")
        }

        if (!manualSetupExpanded && (state.importedProfileInfo != null || hasDraftProfile)) {
            ProfileSummaryCard(
                state = state,
                imported = state.importedProfileInfo != null,
            )
        }

        TextButton(
            onClick = { manualSetupExpanded = !manualSetupExpanded },
            modifier = Modifier.align(Alignment.CenterHorizontally),
            enabled = !state.isBusy,
        ) {
            Text(
                when {
                    manualSetupExpanded -> "Hide manual configuration"
                    state.importedProfileInfo != null -> "Edit fields manually"
                    hasDraftProfile -> "Edit profile fields"
                    else -> "Configure manually instead"
                },
            )
        }

        if (manualSetupExpanded) {
            ManualProfileFields(
                state = state,
                onProfileNameChanged = onProfileNameChanged,
                onServerChanged = onServerChanged,
                onUsernameChanged = onUsernameChanged,
            )
        }

        val showProvisioningControls = state.importedProfileInfo != null ||
            manualSetupExpanded ||
            state.configured ||
            hasDraftProfile
        if (showProvisioningControls) {
            PasswordSection(
                password = password,
                onPasswordChanged = { password = it },
                error = state.fieldErrors[ProfileField.PASSWORD],
                importedProfileReady = state.importedProfileInfo != null,
            )

            CertificateSection(
                state = state,
                onImportCertificate = onImportCertificate,
            )

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
}

@Composable
private fun ProfileSummaryCard(state: VpnUiState, imported: Boolean) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainer,
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                text = if (imported) "Profile imported" else "Profile details",
                style = MaterialTheme.typography.titleMedium,
            )
            Text(
                text = if (imported) "Ready to provision" else "Manual configuration",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
            )
            ProfileSummaryRow("Profile name", state.profileName)
            ProfileSummaryRow("Server", state.serverAddress)
            ProfileSummaryRow("Username", state.username)
            ProfileSummaryRow("Certificate", state.certificateInfo?.displayName.orEmpty())
        }
    }
}

@Composable
private fun ProfileSummaryRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = label,
            modifier = Modifier.weight(0.36f),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = value.ifBlank { "Not set" },
            modifier = Modifier.weight(0.64f),
            style = MaterialTheme.typography.bodyMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
private fun ManualProfileFields(
    state: VpnUiState,
    onProfileNameChanged: (String) -> Unit,
    onServerChanged: (String) -> Unit,
    onUsernameChanged: (String) -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerLow,
        ),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Manual profile details", style = MaterialTheme.typography.titleMedium)
            Text(
                "Use these fields when you do not have an .ikev profile.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
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
        }
    }
}

@Composable
private fun PasswordSection(
    password: String,
    onPasswordChanged: (String) -> Unit,
    error: String?,
    importedProfileReady: Boolean,
) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Password", style = MaterialTheme.typography.titleMedium)
        Text(
            text = if (importedProfileReady) {
                "Your imported profile is ready. Enter the VPN password to finish setup. " +
                    "It is never stored."
            } else {
                "Required to provision this profile. It is never stored."
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedTextField(
            value = password,
            onValueChange = onPasswordChanged,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("VPN Password") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            isError = error != null,
            supportingText = error?.let { message -> { Text(message) } },
        )
    }
}

@Composable
private fun CertificateSection(
    state: VpnUiState,
    onImportCertificate: () -> Unit,
) {
    val certificate = state.certificateInfo
    var detailsExpanded by rememberSaveable(certificate?.sha256Fingerprint) {
        mutableStateOf(false)
    }

    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("CA Certificate", style = MaterialTheme.typography.titleMedium)
        if (certificate == null) {
            Text(
                "A CA certificate is required to verify the VPN server.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedButton(
                onClick = onImportCertificate,
                modifier = Modifier.fillMaxWidth(),
                enabled = !state.isBusy,
            ) {
                Text("Import CA Certificate")
            }
        } else {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceContainer,
                ),
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = certificate.displayName,
                                style = MaterialTheme.typography.titleSmall,
                                maxLines = 2,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Text(
                                text = if (certificate.isCertificateAuthority) {
                                    "✓ Valid CA certificate"
                                } else {
                                    "Not marked as a CA certificate"
                                },
                                color = if (certificate.isCertificateAuthority) {
                                    MaterialTheme.colorScheme.primary
                                } else {
                                    MaterialTheme.colorScheme.error
                                },
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                        TextButton(onClick = { detailsExpanded = !detailsExpanded }) {
                            Text(if (detailsExpanded) "Hide details" else "View details")
                        }
                    }

                    if (detailsExpanded) {
                        HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                        CertificateDetail("Subject", certificate.subject)
                        CertificateDetail("Issuer", certificate.issuer)
                        CertificateDetail("SHA-256", certificate.sha256Fingerprint)
                    }

                    TextButton(
                        onClick = onImportCertificate,
                        modifier = Modifier.align(Alignment.End),
                        enabled = !state.isBusy,
                    ) {
                        Text("Replace CA Certificate")
                    }
                }
            }
        }

        state.fieldErrors[ProfileField.CERTIFICATE]?.let { message ->
            Text(
                text = message,
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun CertificateDetail(label: String, value: String) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(text = value, style = MaterialTheme.typography.bodySmall)
    }
}

private val MANUAL_PROFILE_FIELDS = setOf(
    ProfileField.PROFILE_NAME,
    ProfileField.SERVER_ADDRESS,
    ProfileField.USERNAME,
)

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
    val containerColor = if (isError) {
        MaterialTheme.colorScheme.errorContainer
    } else {
        MaterialTheme.colorScheme.surfaceContainer
    }
    val contentColor = if (isError) {
        MaterialTheme.colorScheme.onErrorContainer
    } else {
        MaterialTheme.colorScheme.onSurface
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = containerColor),
    ) {
        Text(message, modifier = Modifier.padding(16.dp), color = contentColor)
    }
}
