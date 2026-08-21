package com.saeed.ikev2vpn.data

import android.content.Context
import android.util.AtomicFile
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.saeed.ikev2vpn.certificate.CertificateLoader
import com.saeed.ikev2vpn.certificate.LoadedCertificate
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException

private val Context.profileDataStore by preferencesDataStore(name = "vpn_profile")

data class StoredProfile(
    val config: VpnProfileConfig,
    val certificate: LoadedCertificate,
    val status: ProvisioningStatus,
)

data class StoredDiagnostics(
    val lastError: String? = null,
    val lastTechnicalError: String? = null,
    val lastErrorTimestamp: Long? = null,
    val repositoryError: String? = null,
)

data class RepositorySnapshot(
    val profile: StoredProfile? = null,
    val diagnostics: StoredDiagnostics = StoredDiagnostics(),
)

interface ProfileRepository {
    val snapshots: Flow<RepositorySnapshot>

    suspend fun saveProfile(
        config: VpnProfileConfig,
        certificate: LoadedCertificate,
        status: ProvisioningStatus,
    )

    suspend fun setProvisioningStatus(status: ProvisioningStatus)
    suspend fun recordError(userMessage: String, technicalMessage: String)
    suspend fun clearError()
}

class DataStoreProfileRepository(
    context: Context,
    private val certificateLoader: CertificateLoader,
) : ProfileRepository {
    private val applicationContext = context.applicationContext

    override val snapshots: Flow<RepositorySnapshot> = applicationContext.profileDataStore.data
        .catch { exception ->
            if (exception is IOException) emit(emptyPreferences()) else throw exception
        }
        .map(::toSnapshot)
        .flowOn(Dispatchers.IO)

    override suspend fun saveProfile(
        config: VpnProfileConfig,
        certificate: LoadedCertificate,
        status: ProvisioningStatus,
    ) {
        val certificateFile = certificateFileForFingerprint(certificate.info.sha256Fingerprint)
            ?: throw IOException("The CA certificate fingerprint is invalid.")
        withContext(Dispatchers.IO) {
            writeCertificate(certificateFile, certificate.derBytes)
        }
        applicationContext.profileDataStore.edit { preferences ->
            preferences[PROFILE_NAME] = config.profileName
            preferences[SERVER_ADDRESS] = config.serverAddress
            preferences[USERNAME] = config.username
            preferences[CERTIFICATE_FINGERPRINT] = certificate.info.sha256Fingerprint
            preferences[STATUS] = status.name
        }
        withContext(Dispatchers.IO) {
            removeUnreferencedCertificates(certificateFile.baseFile)
        }
    }

    override suspend fun setProvisioningStatus(status: ProvisioningStatus) {
        applicationContext.profileDataStore.edit { preferences ->
            if (preferences[PROFILE_NAME] == null) {
                throw IOException("No stored VPN profile is available to update.")
            }
            preferences[STATUS] = status.name
        }
    }

    override suspend fun recordError(userMessage: String, technicalMessage: String) {
        applicationContext.profileDataStore.edit { preferences ->
            preferences[LAST_ERROR] = userMessage
            preferences[LAST_TECHNICAL_ERROR] = technicalMessage
            preferences[LAST_ERROR_TIMESTAMP] = System.currentTimeMillis()
        }
    }

    override suspend fun clearError() {
        applicationContext.profileDataStore.edit { preferences ->
            preferences.remove(LAST_ERROR)
            preferences.remove(LAST_TECHNICAL_ERROR)
            preferences.remove(LAST_ERROR_TIMESTAMP)
        }
    }

    private fun toSnapshot(preferences: Preferences): RepositorySnapshot {
        val diagnostics = StoredDiagnostics(
            lastError = preferences[LAST_ERROR],
            lastTechnicalError = preferences[LAST_TECHNICAL_ERROR],
            lastErrorTimestamp = preferences[LAST_ERROR_TIMESTAMP],
        )
        val profileName = preferences[PROFILE_NAME] ?: return RepositorySnapshot(diagnostics = diagnostics)
        val serverAddress = preferences[SERVER_ADDRESS] ?: return RepositorySnapshot(diagnostics = diagnostics)
        val username = preferences[USERNAME] ?: return RepositorySnapshot(diagnostics = diagnostics)
        val expectedFingerprint = preferences[CERTIFICATE_FINGERPRINT]
            ?: return RepositorySnapshot(diagnostics = diagnostics)

        val certificateFile = certificateFileForFingerprint(expectedFingerprint)
            ?: return RepositorySnapshot(
                diagnostics = diagnostics.copy(
                    repositoryError = "The stored CA certificate fingerprint is invalid.",
                ),
            )
        val certificate = try {
            certificateLoader.load(certificateFile.readFully())
        } catch (exception: Exception) {
            return RepositorySnapshot(
                diagnostics = diagnostics.copy(
                    repositoryError = "The stored CA certificate could not be validated.",
                ),
            )
        }
        if (certificate.info.sha256Fingerprint != expectedFingerprint) {
            return RepositorySnapshot(
                diagnostics = diagnostics.copy(
                    repositoryError = "The stored CA certificate fingerprint does not match the profile.",
                ),
            )
        }

        val status = runCatching {
            ProvisioningStatus.valueOf(preferences[STATUS] ?: ProvisioningStatus.DRAFT.name)
        }.getOrDefault(ProvisioningStatus.DRAFT)

        return RepositorySnapshot(
            profile = StoredProfile(
                config = VpnProfileConfig(profileName, serverAddress, username),
                certificate = certificate,
                status = status,
            ),
            diagnostics = diagnostics,
        )
    }

    private fun certificateFileForFingerprint(fingerprint: String): AtomicFile? {
        val hexFingerprint = fingerprint.replace(":", "")
        if (!HEX_SHA_256.matches(hexFingerprint)) return null
        return AtomicFile(File(applicationContext.filesDir, "$CERTIFICATE_FILE_PREFIX$hexFingerprint.der"))
    }

    private fun writeCertificate(certificateFile: AtomicFile, bytes: ByteArray) {
        val output = certificateFile.startWrite()
        try {
            output.write(bytes)
            output.fd.sync()
            certificateFile.finishWrite(output)
        } catch (exception: Exception) {
            certificateFile.failWrite(output)
            throw exception
        }
    }

    private fun removeUnreferencedCertificates(activeFile: File) {
        applicationContext.filesDir.listFiles().orEmpty()
            .filter { file ->
                file != activeFile &&
                    file.name.startsWith(CERTIFICATE_FILE_PREFIX) &&
                    file.name.endsWith(".der")
            }
            .forEach { file -> runCatching { file.delete() } }
    }

    private companion object {
        const val CERTIFICATE_FILE_PREFIX = "vpn_ca_"
        val HEX_SHA_256 = Regex("[0-9A-F]{64}")
        val PROFILE_NAME = stringPreferencesKey("profile_name")
        val SERVER_ADDRESS = stringPreferencesKey("server_address")
        val USERNAME = stringPreferencesKey("username")
        val CERTIFICATE_FINGERPRINT = stringPreferencesKey("certificate_fingerprint")
        val STATUS = stringPreferencesKey("provisioning_status")
        val LAST_ERROR = stringPreferencesKey("last_error")
        val LAST_TECHNICAL_ERROR = stringPreferencesKey("last_technical_error")
        val LAST_ERROR_TIMESTAMP = longPreferencesKey("last_error_timestamp")
    }
}
