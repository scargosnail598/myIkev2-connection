package com.saeed.ikev2vpn

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import com.saeed.ikev2vpn.ui.VpnApp
import com.saeed.ikev2vpn.ui.VpnViewModel
import com.saeed.ikev2vpn.ui.theme.Ikev2VpnTheme

class MainActivity : ComponentActivity() {
    private val viewModel: VpnViewModel by viewModels {
        val application = application as Ikev2VpnApplication
        VpnViewModel.Factory(
            profileRepository = application.profileRepository,
            certificateImporter = application.certificateImporter,
            ikevProfileImporter = application.ikevProfileImporter,
            vpnController = application.vpnController,
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            Ikev2VpnTheme {
                VpnApp(viewModel)
            }
        }
    }
}
