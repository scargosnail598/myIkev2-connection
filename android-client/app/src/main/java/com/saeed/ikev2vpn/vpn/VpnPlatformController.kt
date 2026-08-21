package com.saeed.ikev2vpn.vpn

import com.saeed.ikev2vpn.data.VpnProfileConfig
import kotlinx.coroutines.flow.StateFlow
import java.security.cert.X509Certificate

interface VpnPlatformController {
    val state: StateFlow<VpnState>

    fun isPlatformSupported(): Boolean

    fun provision(
        config: VpnProfileConfig,
        password: String,
        serverRootCa: X509Certificate,
    ): VpnResult<ProvisioningAction>

    fun connect(): VpnResult<String?>
    fun disconnect(): VpnResult<Unit>
    fun deleteProvisionedProfile(): VpnResult<Unit>
    fun refreshState()
}
