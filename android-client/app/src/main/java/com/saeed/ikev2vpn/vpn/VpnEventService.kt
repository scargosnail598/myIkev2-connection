package com.saeed.ikev2vpn.vpn

import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.saeed.ikev2vpn.Ikev2VpnApplication
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

class VpnEventService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || intent == null) {
            stopSelfResult(startId)
            return START_NOT_STICKY
        }

        val application = application as Ikev2VpnApplication
        val diagnostic = application.vpnController.handleVpnManagerEvent(intent)
        serviceScope.launch {
            diagnostic.userMessage?.let { userMessage ->
                runCatching {
                    application.profileRepository.recordError(userMessage, diagnostic.technicalMessage)
                }
            }
            stopSelfResult(startId)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        serviceScope.cancel()
        super.onDestroy()
    }
}
