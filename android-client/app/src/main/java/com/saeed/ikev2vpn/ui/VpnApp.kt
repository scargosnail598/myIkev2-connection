package com.saeed.ikev2vpn.ui

import android.app.Activity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner

@Composable
fun VpnApp(viewModel: VpnViewModel) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    val lifecycleOwner = LocalLifecycleOwner.current
    val certificatePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenDocument(),
    ) { uri ->
        uri?.let(viewModel::importCertificate)
    }
    val ikevProfilePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenDocument(),
    ) { uri ->
        uri?.let(viewModel::importIkevProfile)
    }
    val consentLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        viewModel.onVpnConsentResult(result.resultCode == Activity.RESULT_OK)
    }

    LaunchedEffect(viewModel) {
        viewModel.effects.collect { effect ->
            when (effect) {
                is VpnUiEffect.RequestVpnConsent -> consentLauncher.launch(effect.intent)
            }
        }
    }

    DisposableEffect(lifecycleOwner, viewModel) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) viewModel.refreshState()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    BackHandler(
        enabled = state.screen != AppScreen.MAIN && (state.configured || state.isBusy),
    ) {
        if (!state.isBusy) viewModel.showMain()
    }

    if (!state.initialized) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            CircularProgressIndicator()
        }
        return
    }

    when (state.screen) {
        AppScreen.SETUP -> SetupScreen(
            state = state,
            onProfileNameChanged = viewModel::updateProfileName,
            onServerChanged = viewModel::updateServerAddress,
            onUsernameChanged = viewModel::updateUsername,
            onImportIkevProfile = { ikevProfilePicker.launch(arrayOf("*/*")) },
            onImportCertificate = { certificatePicker.launch(arrayOf("*/*")) },
            onProvision = viewModel::provisionProfile,
            onBack = viewModel::showMain,
            onDismissError = viewModel::dismissError,
        )
        AppScreen.MAIN -> MainScreen(
            state = state,
            onConnect = viewModel::connect,
            onDisconnect = viewModel::disconnect,
            onEdit = viewModel::showSetup,
            onDiagnostics = viewModel::showDiagnostics,
            onDismissError = viewModel::dismissError,
        )
        AppScreen.DIAGNOSTICS -> DiagnosticsScreen(
            state = state,
            onBack = viewModel::showMain,
        )
    }
}
