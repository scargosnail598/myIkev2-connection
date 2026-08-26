package com.saeed.ikev2vpn.ui

import android.net.Uri
import com.saeed.ikev2vpn.certificate.CertificateImporter
import com.saeed.ikev2vpn.certificate.CertificateLoader
import com.saeed.ikev2vpn.certificate.CertificateLoaderTest
import com.saeed.ikev2vpn.certificate.LoadedCertificate
import com.saeed.ikev2vpn.data.ProfileRepository
import com.saeed.ikev2vpn.data.ProvisioningStatus
import com.saeed.ikev2vpn.data.RepositorySnapshot
import com.saeed.ikev2vpn.data.StoredDiagnostics
import com.saeed.ikev2vpn.data.StoredProfile
import com.saeed.ikev2vpn.data.VpnProfileConfig
import com.saeed.ikev2vpn.profile.IkevProfileImporter
import com.saeed.ikev2vpn.profile.ImportedIkevProfile
import com.saeed.ikev2vpn.profile.ImportedProxyMetadata
import com.saeed.ikev2vpn.vpn.ConnectionState
import com.saeed.ikev2vpn.vpn.ProvisioningAction
import com.saeed.ikev2vpn.vpn.VpnPlatformController
import com.saeed.ikev2vpn.vpn.VpnResult
import com.saeed.ikev2vpn.vpn.VpnState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.mockito.Mockito.mock
import java.security.cert.X509Certificate
import java.time.Instant
import java.util.Base64
import java.util.Date

@OptIn(ExperimentalCoroutinesApi::class)
class VpnViewModelTest {
    private val mainDispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(mainDispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `canceling edit restores the committed profile`() = runTest {
        val certificate = testCertificate()
        val repository = FakeProfileRepository(provisionedSnapshot(certificate))
        val viewModel = createViewModel(repository, FakeVpnController())

        viewModel.showSetup()
        viewModel.updateProfileName("Unsaved name")
        viewModel.updateServerAddress("draft.example.com")
        viewModel.updateUsername("draft-user")
        viewModel.showMain()

        val state = viewModel.uiState.value
        assertEquals(AppScreen.MAIN, state.screen)
        assertEquals("Saved VPN", state.profileName)
        assertEquals("vpn.example.com", state.serverAddress)
        assertEquals("saved-user", state.username)
        assertEquals(certificate.info.sha256Fingerprint, state.certificateInfo?.sha256Fingerprint)
    }

    @Test
    fun `editing is blocked while the VPN is connected`() = runTest {
        val repository = FakeProfileRepository(provisionedSnapshot(testCertificate()))
        val controller = FakeVpnController(
            VpnState(connectionState = ConnectionState.CONNECTED, confirmed = true),
        )
        val viewModel = createViewModel(repository, controller)

        viewModel.showSetup()

        assertEquals(AppScreen.MAIN, viewModel.uiState.value.screen)
        assertTrue(viewModel.uiState.value.error.orEmpty().contains("Disconnect"))
    }

    @Test
    fun `storage failure removes the platform profile and leaves an unconfigured draft`() = runTest {
        val repository = FakeProfileRepository(provisionedSnapshot(testCertificate())).apply {
            failSave = true
        }
        val controller = FakeVpnController()
        val viewModel = createViewModel(repository, controller)
        viewModel.showSetup()

        viewModel.provisionProfile("temporary-password")

        val state = viewModel.uiState.value
        assertEquals(1, controller.deleteCalls)
        assertFalse(state.configured)
        assertEquals(ProvisioningStatus.DRAFT, state.provisioningStatus)
        assertEquals(ConnectionState.NOT_CONFIGURED, state.connectionState)
        assertFalse(state.toString().contains("temporary-password"))
    }

    @Test
    fun `portable import populates setup without provisioning or persistence`() = runTest {
        val certificate = testCertificate()
        val repository = FakeProfileRepository(RepositorySnapshot())
        val controller = FakeVpnController()
        val imported = ImportedIkevProfile(
            config = VpnProfileConfig("saeed@155.117.13.45", "155.117.13.45", "saeed"),
            certificate = certificate,
            remoteId = "155.117.13.45",
            serverProfile = "secure",
            proxy = ImportedProxyMetadata(true, "socks5", "10.254.254.1", 1080),
        )
        val viewModel = createViewModel(repository, controller, imported)
        val uri = mock(Uri::class.java)

        viewModel.importIkevProfile(uri)

        val state = viewModel.uiState.value
        assertEquals("saeed@155.117.13.45", state.profileName)
        assertEquals("155.117.13.45", state.serverAddress)
        assertEquals("saeed", state.username)
        assertEquals(certificate.info.sha256Fingerprint, state.certificateInfo?.sha256Fingerprint)
        assertTrue(state.importedProfileInfo?.proxySummary.orEmpty().contains("10.254.254.1:1080"))
        assertEquals(0, controller.provisionCalls)
        assertEquals(0, repository.saveCalls)
        assertFalse(state.toString().contains("password", ignoreCase = true))
    }

    @Test
    fun `portable import is blocked while connected`() = runTest {
        val repository = FakeProfileRepository(provisionedSnapshot(testCertificate()))
        val controller = FakeVpnController(
            VpnState(connectionState = ConnectionState.CONNECTED, confirmed = true),
        )
        var importerCalls = 0
        val viewModel = createViewModel(
            repository = repository,
            controller = controller,
            onIkevProfileImport = { importerCalls += 1 },
        )
        val uri = mock(Uri::class.java)

        viewModel.importIkevProfile(uri)

        assertTrue(viewModel.uiState.value.error.orEmpty().contains("Disconnect"))
        assertEquals(0, controller.provisionCalls)
        assertEquals("Saved VPN", viewModel.uiState.value.profileName)
        assertEquals(0, importerCalls)
    }

    private fun createViewModel(
        repository: ProfileRepository,
        controller: VpnPlatformController,
        importedProfile: ImportedIkevProfile? = null,
        onIkevProfileImport: () -> Unit = {},
    ): VpnViewModel = VpnViewModel(
        profileRepository = repository,
        certificateImporter = object : CertificateImporter {
            override suspend fun import(uri: Uri): LoadedCertificate = error("Not used in this test")
        },
        ikevProfileImporter = object : IkevProfileImporter {
            override suspend fun import(uri: Uri): ImportedIkevProfile {
                onIkevProfileImport()
                return importedProfile ?: error("Not used in this test")
            }
        },
        vpnController = controller,
    )

    private fun provisionedSnapshot(certificate: LoadedCertificate) = RepositorySnapshot(
        profile = StoredProfile(
            config = VpnProfileConfig("Saved VPN", "vpn.example.com", "saved-user"),
            certificate = certificate,
            status = ProvisioningStatus.PROVISIONED,
        ),
    )

    private fun testCertificate(): LoadedCertificate {
        val loader = CertificateLoader {
            Date.from(Instant.parse("2030-01-01T00:00:00Z"))
        }
        return loader.load(Base64.getDecoder().decode(CertificateLoaderTest.DER_BASE64))
    }

    private class FakeProfileRepository(initial: RepositorySnapshot) : ProfileRepository {
        private val mutableSnapshots = MutableStateFlow(initial)
        override val snapshots: Flow<RepositorySnapshot> = mutableSnapshots
        var failSave = false
        var saveCalls = 0

        override suspend fun saveProfile(
            config: VpnProfileConfig,
            certificate: LoadedCertificate,
            status: ProvisioningStatus,
        ) {
            saveCalls += 1
            if (failSave) error("Simulated storage failure")
            mutableSnapshots.value = RepositorySnapshot(StoredProfile(config, certificate, status))
        }

        override suspend fun setProvisioningStatus(status: ProvisioningStatus) {
            val profile = mutableSnapshots.value.profile ?: error("No profile")
            mutableSnapshots.value = mutableSnapshots.value.copy(profile = profile.copy(status = status))
        }

        override suspend fun recordError(userMessage: String, technicalMessage: String) {
            mutableSnapshots.value = mutableSnapshots.value.copy(
                diagnostics = StoredDiagnostics(
                    lastError = userMessage,
                    lastTechnicalError = technicalMessage,
                ),
            )
        }

        override suspend fun clearError() {
            mutableSnapshots.value = mutableSnapshots.value.copy(diagnostics = StoredDiagnostics())
        }
    }

    private class FakeVpnController(
        initialState: VpnState = VpnState(connectionState = ConnectionState.DISCONNECTED),
    ) : VpnPlatformController {
        private val mutableState = MutableStateFlow(initialState)
        override val state = mutableState
        var deleteCalls = 0
        var provisionCalls = 0

        override fun isPlatformSupported(): Boolean = true

        override fun provision(
            config: VpnProfileConfig,
            password: String,
            serverRootCa: X509Certificate,
        ): VpnResult<ProvisioningAction> {
            provisionCalls += 1
            return VpnResult.Success(ProvisioningAction.Complete)
        }

        override fun connect(): VpnResult<String?> = VpnResult.Success(null)
        override fun disconnect(): VpnResult<Unit> = VpnResult.Success(Unit)

        override fun deleteProvisionedProfile(): VpnResult<Unit> {
            deleteCalls += 1
            return VpnResult.Success(Unit)
        }

        override fun refreshState() = Unit
    }
}
