package com.saeed.ikev2vpn.vpn

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Ikev2VpnProfile
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.VpnManager
import android.net.VpnProfileState
import android.os.Build
import android.os.Process
import androidx.annotation.RequiresApi
import com.saeed.ikev2vpn.data.VpnProfileConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.security.cert.X509Certificate
import java.util.Collections

class AndroidVpnPlatformController(context: Context) : VpnPlatformController {
    private val applicationContext = context.applicationContext
    private val vpnManager = applicationContext.getSystemService(VpnManager::class.java)
    private val connectivityManager = applicationContext.getSystemService(ConnectivityManager::class.java)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val ownedVpnNetworks = Collections.synchronizedSet(mutableSetOf<Network>())
    private val mutableState = MutableStateFlow(VpnState())
    private var transitionJob: Job? = null
    @Volatile
    private var recoverableRetryActive = false

    override val state: StateFlow<VpnState> = mutableState.asStateFlow()

    init {
        registerOwnedVpnNetworkCallback()
        refreshState()
    }

    override fun isPlatformSupported(): Boolean {
        return applicationContext.packageManager.hasSystemFeature(PackageManager.FEATURE_IPSEC_TUNNELS)
    }

    override fun provision(
        config: VpnProfileConfig,
        password: String,
        serverRootCa: X509Certificate,
    ): VpnResult<ProvisioningAction> {
        if (!isPlatformSupported()) {
            return VpnResult.Failure(
                "The device does not support platform IPsec tunnels.",
                "PackageManager.FEATURE_IPSEC_TUNNELS is absent.",
            )
        }

        return try {
            // serverAddress is Android's remote IKE identity (IDr); the second constructor
            // argument is the local IKE identity (IDi), so use the EAP username here.
            val builder = Ikev2VpnProfile.Builder(config.serverAddress, config.username)
                .setAuthUsernamePassword(config.username, password, serverRootCa)
                .setBypassable(false)
                .setMetered(false)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                Api33.configureInternetValidation(builder)
            }
            val consentIntent = vpnManager.provisionVpnProfile(builder.build())
            VpnResult.Success(
                consentIntent?.let(ProvisioningAction::ConsentRequired)
                    ?: ProvisioningAction.Complete,
            )
        } catch (exception: Exception) {
            failure("Android rejected the IKEv2 VPN profile.", exception)
        }
    }

    override fun connect(): VpnResult<String?> {
        if (!isPlatformSupported()) {
            return VpnResult.Failure(
                "The device does not support platform IPsec tunnels.",
                "PackageManager.FEATURE_IPSEC_TUNNELS is absent.",
            )
        }

        recoverableRetryActive = false
        reduceState(VpnStateEvent.ConnectRequested)
        return try {
            val sessionId = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                Api33.start(vpnManager)
            } else {
                Api30.start(vpnManager)
                null
            }
            mutableState.update { it.copy(sessionId = sessionId) }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                pollApi33State(expectDisconnected = false)
            } else if (ownedVpnNetworks.isNotEmpty()) {
                reduceState(VpnStateEvent.OwnedNetworkAvailable)
            } else {
                scheduleLegacyConnectionTimeout()
            }
            VpnResult.Success(sessionId)
        } catch (exception: Exception) {
            val result = failure("Android could not start the VPN profile.", exception)
            reduceState(VpnStateEvent.Failed(result.userMessage, StateEvidence.LOCAL_REQUEST, false))
            result
        }
    }

    override fun disconnect(): VpnResult<Unit> {
        recoverableRetryActive = false
        reduceState(VpnStateEvent.DisconnectRequested)
        transitionJob?.cancel()
        return try {
            vpnManager.stopProvisionedVpnProfile()
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                pollApi33State(expectDisconnected = true)
            } else if (ownedVpnNetworks.isEmpty()) {
                transitionJob = scope.launch {
                    delay(500)
                    reduceState(VpnStateEvent.LocalDisconnectCompleted)
                }
            } else {
                scheduleLegacyDisconnectTimeout()
            }
            VpnResult.Success(Unit)
        } catch (exception: Exception) {
            val result = failure("Android could not stop the VPN profile.", exception)
            reduceState(VpnStateEvent.Failed(result.userMessage, StateEvidence.LOCAL_REQUEST, false))
            result
        }
    }

    override fun deleteProvisionedProfile(): VpnResult<Unit> = try {
        vpnManager.deleteProvisionedVpnProfile()
        VpnResult.Success(Unit)
    } catch (exception: Exception) {
        failure("Android could not remove the incomplete VPN profile.", exception)
    }

    override fun refreshState() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            try {
                applyApi33State(Api33.getState(vpnManager))
            } catch (_: RuntimeException) {
                reduceState(
                    VpnStateEvent.Failed(
                        "Android VPN state is unavailable.",
                        StateEvidence.API_33_PROFILE_STATE,
                        false,
                    ),
                )
            }
        } else if (ownedVpnNetworks.isNotEmpty()) {
            reduceState(VpnStateEvent.OwnedNetworkAvailable)
        }
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    fun handleVpnManagerEvent(intent: Intent): VpnEventDiagnostic {
        if (intent.action != VpnManager.ACTION_VPN_MANAGER_EVENT) {
            return VpnEventDiagnostic(null, "Ignored an unrelated service intent.")
        }

        val eventSessionId = intent.getStringExtra(VpnManager.EXTRA_SESSION_KEY)
        val activeSessionId = mutableState.value.sessionId
        if (eventSessionId != null && activeSessionId != null && eventSessionId != activeSessionId) {
            return VpnEventDiagnostic(null, "Ignored an event for a stale VPN session.")
        }

        val categories = intent.categories.orEmpty()
        if (VpnManager.CATEGORY_EVENT_DEACTIVATED_BY_USER in categories) {
            recoverableRetryActive = false
            transitionJob?.cancel()
            mutableState.update {
                VpnState(
                    connectionState = ConnectionState.DISCONNECTED,
                    evidence = StateEvidence.API_33_EVENT,
                    confirmed = true,
                    detail = "Android reports that the VPN was deactivated by the user.",
                )
            }
            return VpnEventDiagnostic(null, "VPN deactivated by user.")
        }

        if (VpnManager.CATEGORY_EVENT_ALWAYS_ON_STATE_CHANGED in categories) {
            refreshState()
            return VpnEventDiagnostic(null, "Always-on VPN state changed.")
        }

        val errorClass = intent.getIntExtra(VpnManager.EXTRA_ERROR_CLASS, -1)
        val errorCode = intent.getIntExtra(VpnManager.EXTRA_ERROR_CODE, -1)
        val userMessage = when {
            VpnManager.CATEGORY_EVENT_NETWORK_ERROR in categories -> networkErrorMessage(errorCode)
            VpnManager.CATEGORY_EVENT_IKE_ERROR in categories && errorCode == IKE_AUTHENTICATION_FAILED ->
                "Authentication or server certificate validation failed."
            VpnManager.CATEGORY_EVENT_IKE_ERROR in categories ->
                "Android reported an IKEv2 protocol error."
            else -> "Android reported an unknown VPN error."
        }
        val technical = "VpnManager event categories=$categories errorClass=$errorClass errorCode=$errorCode"
        if (errorClass == VpnManager.ERROR_CLASS_RECOVERABLE) {
            recoverableRetryActive = true
            reduceState(VpnStateEvent.Recovering(userMessage))
            pollApi33State(expectDisconnected = false)
            return VpnEventDiagnostic(userMessage, "$technical recoverable=true")
        }
        recoverableRetryActive = false
        transitionJob?.cancel()
        reduceState(VpnStateEvent.Failed(userMessage, StateEvidence.API_33_EVENT, true))
        return VpnEventDiagnostic(userMessage, technical)
    }

    private fun registerOwnedVpnNetworkCallback() {
        val request = NetworkRequest.Builder()
            .clearCapabilities()
            .addTransportType(NetworkCapabilities.TRANSPORT_VPN)
            .build()
        val callback = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            Api31OwnedVpnCallback(::onCapabilitiesChanged, ::onNetworkLost)
        } else {
            LegacyOwnedVpnCallback(::onCapabilitiesChanged, ::onNetworkLost)
        }
        try {
            connectivityManager.registerNetworkCallback(request, callback)
        } catch (exception: RuntimeException) {
            mutableState.update {
                it.copy(detail = "VPN network observation is unavailable: ${exception.javaClass.simpleName}")
            }
        }
    }

    private fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
        if (capabilities.ownerUid == Process.myUid()) {
            ownedVpnNetworks += network
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
                transitionJob?.cancel()
                reduceState(VpnStateEvent.OwnedNetworkAvailable)
            }
        } else {
            ownedVpnNetworks -= network
        }
    }

    private fun onNetworkLost(network: Network) {
        val wasOwned = ownedVpnNetworks.remove(network)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU &&
            wasOwned &&
            ownedVpnNetworks.isEmpty()
        ) {
            reduceState(VpnStateEvent.OwnedNetworkLost)
        }
    }

    private fun scheduleLegacyConnectionTimeout() {
        transitionJob?.cancel()
        transitionJob = scope.launch {
            delay(CONNECTION_TIMEOUT_MILLIS)
            if (mutableState.value.connectionState == ConnectionState.CONNECTING) {
                reduceState(
                    VpnStateEvent.ConfirmationTimedOut(
                        "The VPN connection was not confirmed by Android. Check the server and credentials.",
                        StateEvidence.LOCAL_REQUEST,
                    ),
                )
            }
        }
    }

    private fun scheduleLegacyDisconnectTimeout() {
        transitionJob?.cancel()
        transitionJob = scope.launch {
            delay(CONNECTION_TIMEOUT_MILLIS)
            if (mutableState.value.connectionState == ConnectionState.DISCONNECTING) {
                reduceState(
                    VpnStateEvent.ConfirmationTimedOut(
                        "Android did not confirm that the VPN network was removed. Stop or reset it before reconnecting.",
                        StateEvidence.LOCAL_REQUEST,
                    ),
                )
            }
        }
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun pollApi33State(expectDisconnected: Boolean) {
        transitionJob?.cancel()
        transitionJob = scope.launch {
            var attempts = 0
            while (true) {
                val profileState = try {
                    Api33.getState(vpnManager)
                } catch (_: RuntimeException) {
                    reduceState(
                        VpnStateEvent.Failed(
                            "Android VPN state is unavailable.",
                            StateEvidence.API_33_PROFILE_STATE,
                            false,
                        ),
                    )
                    return@launch
                }
                if (profileState != null) {
                    applyApi33State(profileState)
                    val state = mutableState.value.connectionState
                    if ((!expectDisconnected && state in setOf(ConnectionState.CONNECTED, ConnectionState.ERROR)) ||
                        (expectDisconnected && state == ConnectionState.DISCONNECTED)
                    ) {
                        return@launch
                    }
                } else if (expectDisconnected) {
                    applyApi33State(null)
                    return@launch
                }
                delay(API_33_POLL_INTERVAL_MILLIS)
                attempts += 1
                if (attempts >= API_33_POLL_ATTEMPTS) {
                    if (recoverableRetryActive && !expectDisconnected) {
                        attempts = 0
                    } else {
                        if (mutableState.value.connectionState in setOf(
                                ConnectionState.CONNECTING,
                                ConnectionState.DISCONNECTING,
                            )
                        ) {
                            reduceState(
                                VpnStateEvent.ConfirmationTimedOut(
                                    "Android did not confirm the requested VPN state change.",
                                    StateEvidence.API_33_PROFILE_STATE,
                                ),
                            )
                        }
                        return@launch
                    }
                }
            }
        }
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun applyApi33State(profileState: VpnProfileState?) {
        val mappedState = if (profileState == null) {
            ConnectionState.DISCONNECTED
        } else when (profileState.state) {
            VpnProfileState.STATE_CONNECTED -> ConnectionState.CONNECTED
            VpnProfileState.STATE_CONNECTING -> ConnectionState.CONNECTING
            VpnProfileState.STATE_FAILED -> ConnectionState.ERROR
            VpnProfileState.STATE_DISCONNECTED -> ConnectionState.DISCONNECTED
            else -> ConnectionState.UNKNOWN
        }
        if (!VpnStateReducer.shouldApplyPlatformObservation(
                current = mutableState.value.connectionState,
                observed = mappedState,
            )
        ) {
            return
        }
        if (mappedState in setOf(
                ConnectionState.CONNECTED,
                ConnectionState.DISCONNECTED,
                ConnectionState.ERROR,
            )
        ) {
            recoverableRetryActive = false
        }
        reduceState(VpnStateEvent.PlatformStateObserved(mappedState, profileState?.sessionId))
    }

    private fun reduceState(event: VpnStateEvent) {
        mutableState.update { current -> VpnStateReducer.reduce(current, event) }
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private fun networkErrorMessage(errorCode: Int): String = when (errorCode) {
        VpnManager.ERROR_CODE_NETWORK_UNKNOWN_HOST -> "The VPN server name could not be resolved."
        VpnManager.ERROR_CODE_NETWORK_PROTOCOL_TIMEOUT -> "The VPN server could not be reached."
        VpnManager.ERROR_CODE_NETWORK_LOST -> "The network carrying the VPN connection was lost."
        VpnManager.ERROR_CODE_NETWORK_IO -> "A network error interrupted the VPN connection."
        else -> "Android reported a VPN network error."
    }

    private fun failure(userMessage: String, exception: Exception): VpnResult.Failure {
        return VpnResult.Failure(
            userMessage = when (exception) {
                is SecurityException -> "VPN permission is missing or blocked by device policy."
                is IllegalArgumentException -> "Android rejected the IKEv2 VPN profile."
                else -> userMessage
            },
            technicalMessage = "${exception.javaClass.name}: ${exception.message.orEmpty()}",
        )
    }

    private class LegacyOwnedVpnCallback(
        private val capabilitiesChanged: (Network, NetworkCapabilities) -> Unit,
        private val networkLost: (Network) -> Unit,
    ) : ConnectivityManager.NetworkCallback() {
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            capabilitiesChanged(network, capabilities)
        }

        override fun onLost(network: Network) {
            networkLost(network)
        }
    }

    @RequiresApi(Build.VERSION_CODES.S)
    private class Api31OwnedVpnCallback(
        private val capabilitiesChanged: (Network, NetworkCapabilities) -> Unit,
        private val networkLost: (Network) -> Unit,
    ) : ConnectivityManager.NetworkCallback(FLAG_INCLUDE_LOCATION_INFO) {
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            capabilitiesChanged(network, capabilities)
        }

        override fun onLost(network: Network) {
            networkLost(network)
        }
    }

    private object Api30 {
        @Suppress("DEPRECATION")
        fun start(vpnManager: VpnManager) {
            vpnManager.startProvisionedVpnProfile()
        }
    }

    @RequiresApi(Build.VERSION_CODES.TIRAMISU)
    private object Api33 {
        fun configureInternetValidation(builder: Ikev2VpnProfile.Builder) {
            builder.setRequiresInternetValidation(true)
        }

        fun start(vpnManager: VpnManager): String = vpnManager.startProvisionedVpnProfileSession()

        fun getState(vpnManager: VpnManager): VpnProfileState? =
            vpnManager.provisionedVpnProfileState
    }

    private companion object {
        const val IKE_AUTHENTICATION_FAILED = 24
        const val CONNECTION_TIMEOUT_MILLIS = 30_000L
        const val API_33_POLL_INTERVAL_MILLIS = 500L
        const val API_33_POLL_ATTEMPTS = 60
    }
}
