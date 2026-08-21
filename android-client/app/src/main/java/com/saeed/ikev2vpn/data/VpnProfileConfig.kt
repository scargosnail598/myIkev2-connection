package com.saeed.ikev2vpn.data

data class VpnProfileConfig(
    val profileName: String,
    val serverAddress: String,
    val username: String,
)

enum class ProvisioningStatus {
    DRAFT,
    PENDING_CONSENT,
    PROVISIONED,
}
