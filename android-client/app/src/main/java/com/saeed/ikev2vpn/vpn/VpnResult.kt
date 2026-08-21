package com.saeed.ikev2vpn.vpn

import android.content.Intent

sealed interface VpnResult<out T> {
    data class Success<T>(val value: T) : VpnResult<T>
    data class Failure(
        val userMessage: String,
        val technicalMessage: String,
    ) : VpnResult<Nothing>
}

sealed interface ProvisioningAction {
    data object Complete : ProvisioningAction
    data class ConsentRequired(val intent: Intent) : ProvisioningAction
}

data class VpnEventDiagnostic(
    val userMessage: String?,
    val technicalMessage: String,
)
