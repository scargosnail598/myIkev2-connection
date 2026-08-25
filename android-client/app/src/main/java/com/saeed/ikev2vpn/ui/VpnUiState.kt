package com.saeed.ikev2vpn.ui

import com.saeed.ikev2vpn.certificate.CertificateInfo
import com.saeed.ikev2vpn.data.ProvisioningStatus
import com.saeed.ikev2vpn.validation.ProfileField
import com.saeed.ikev2vpn.vpn.ConnectionState
import com.saeed.ikev2vpn.vpn.StateEvidence

enum class AppScreen {
    SETUP,
    MAIN,
    DIAGNOSTICS,
}

data class ImportedProfileUiInfo(
    val remoteId: String,
    val serverProfile: String,
    val proxySummary: String,
    val importRevision: Int,
)

data class VpnUiState(
    val initialized: Boolean = false,
    val screen: AppScreen = AppScreen.SETUP,
    val configured: Boolean = false,
    val provisioningStatus: ProvisioningStatus? = null,
    val connectionState: ConnectionState = ConnectionState.NOT_CONFIGURED,
    val stateEvidence: StateEvidence = StateEvidence.NONE,
    val stateConfirmed: Boolean = false,
    val stateDetail: String = "No VPN profile is configured.",
    val platformSupported: Boolean = false,
    val profileName: String = "",
    val serverAddress: String = "",
    val username: String = "",
    val certificateInfo: CertificateInfo? = null,
    val importedProfileInfo: ImportedProfileUiInfo? = null,
    val sessionId: String? = null,
    val error: String? = null,
    val technicalError: String? = null,
    val lastVpnError: String? = null,
    val lastTechnicalError: String? = null,
    val lastErrorTimestamp: Long? = null,
    val repositoryError: String? = null,
    val fieldErrors: Map<ProfileField, String> = emptyMap(),
    val isBusy: Boolean = false,
)
