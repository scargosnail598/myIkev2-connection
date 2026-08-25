package com.saeed.ikev2vpn.profile

import android.content.ContentResolver
import android.net.Uri
import com.saeed.ikev2vpn.certificate.CertificateLoader
import com.saeed.ikev2vpn.certificate.LoadedCertificate
import com.saeed.ikev2vpn.data.VpnProfileConfig
import com.saeed.ikev2vpn.validation.ProfileValidator
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.util.Base64

data class ImportedIkevProfile(
    val config: VpnProfileConfig,
    val certificate: LoadedCertificate,
    val remoteId: String,
    val serverProfile: String,
    val proxy: ImportedProxyMetadata,
)

data class ImportedProxyMetadata(
    val enabled: Boolean,
    val type: String? = null,
    val host: String? = null,
    val port: Int? = null,
)

interface IkevProfileImporter {
    suspend fun import(uri: Uri): ImportedIkevProfile
}

class AndroidIkevProfileImporter(
    private val contentResolver: ContentResolver,
    certificateLoader: CertificateLoader,
) : IkevProfileImporter {
    private val parser = IkevProfileParser(certificateLoader)

    override suspend fun import(uri: Uri): ImportedIkevProfile = withContext(Dispatchers.IO) {
        val bytes = try {
            contentResolver.openInputStream(uri)?.use(::readBoundedIkevProfile)
                ?: throw IkevProfileImportException("The .ikev profile could not be read.")
        } catch (exception: IkevProfileImportException) {
            throw exception
        } catch (exception: IOException) {
            throw IkevProfileImportException("The .ikev profile could not be read.", exception)
        } catch (exception: SecurityException) {
            throw IkevProfileImportException("Access to the .ikev profile was denied.", exception)
        }

        parser.parse(bytes)
    }
}

class IkevProfileParser(
    private val certificateLoader: CertificateLoader,
) {
    fun parse(input: ByteArray): ImportedIkevProfile {
        if (input.size > MAX_IKEV_PROFILE_BYTES) {
            throw IkevProfileImportException("The .ikev profile is unexpectedly large.")
        }

        val jsonText = try {
            StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(input))
                .toString()
        } catch (exception: Exception) {
            throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE, exception)
        }
        val profile = try {
            val tokener = JSONTokener(jsonText)
            val parsed = tokener.nextValue()
            if (parsed !is JSONObject || tokener.nextClean() != 0.toChar()) {
                throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
            }
            parsed
        } catch (exception: IkevProfileImportException) {
            throw exception
        } catch (exception: JSONException) {
            throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE, exception)
        }

        if (requiredString(profile, "format") != "ikev-profile") {
            throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
        }

        val version = requiredInteger(profile, "version")
        if (version > SUPPORTED_FORMAT_VERSION) {
            throw IkevProfileImportException(
                "This profile uses .ikev format version $version.\n" +
                    "Android client v1.1.0 supports version 1 only.",
            )
        }
        if (version != SUPPORTED_FORMAT_VERSION.toLong()) {
            throw IkevProfileImportException(
                "Unsupported .ikev format version: $version.\n" +
                    "Android client v1.1.0 supports version 1 only.",
            )
        }

        val name = requiredString(profile, "name")
        if (!ProfileValidator.isValidProfileName(name)) {
            throw IkevProfileImportException("The .ikev profile name is invalid.")
        }

        val server = requiredString(profile, "server")
        if (!ProfileValidator.isValidServerAddress(server)) {
            throw IkevProfileImportException("The .ikev server address is invalid.")
        }
        val remoteId = requiredString(profile, "remote_id")
        if (remoteId != server) {
            throw IkevProfileImportException(
                "This .ikev profile uses a Remote ID different from the server address.\n" +
                    "Android client v1.1.0 cannot safely represent that profile.",
            )
        }

        val username = requiredString(profile, "username")
        if (!USERNAME_PATTERN.matches(username)) {
            throw IkevProfileImportException("The .ikev username is invalid.")
        }

        if (requiredString(profile, "authentication") != "eap-mschapv2") {
            throw IkevProfileImportException(
                "This .ikev profile uses an unsupported authentication method.",
            )
        }

        val connection = requiredObject(profile, "connection")
        if (requiredString(connection, "mode") != "full-tunnel") {
            throw IkevProfileImportException("This .ikev profile uses an unsupported connection mode.")
        }

        val serverProfile = requiredString(profile, "server_profile")
        if (serverProfile !in SUPPORTED_SERVER_PROFILES) {
            throw IkevProfileImportException("This .ikev profile uses an unsupported server profile.")
        }

        val ca = requiredObject(profile, "ca_certificate")
        if (requiredString(ca, "encoding") != "der-base64") {
            throw IkevProfileImportException(
                "This .ikev profile uses an unsupported CA certificate encoding.",
            )
        }
        val encodedCertificate = requiredString(ca, "data")
        val expectedFingerprint = requiredString(ca, "sha256")
        if (!SHA256_FINGERPRINT_PATTERN.matches(expectedFingerprint)) {
            throw IkevProfileImportException("The embedded CA certificate fingerprint is invalid.")
        }

        val certificateBytes = try {
            Base64.getDecoder().decode(encodedCertificate)
        } catch (exception: IllegalArgumentException) {
            throw IkevProfileImportException("The embedded CA certificate is invalid.", exception)
        }
        val loadedCertificate = certificateLoader.load(certificateBytes)
        if (!certificateBytes.contentEquals(loadedCertificate.derBytes)) {
            throw IkevProfileImportException("The embedded CA certificate is invalid.")
        }
        if (!loadedCertificate.info.isCertificateAuthority) {
            throw IkevProfileImportException("The embedded certificate is not a CA certificate.")
        }
        if (!loadedCertificate.info.sha256Fingerprint.equals(expectedFingerprint, ignoreCase = true)) {
            throw IkevProfileImportException(
                "CA certificate fingerprint verification failed.\n" +
                    "The profile may be corrupted or modified.",
            )
        }

        val proxy = parseProxy(requiredObject(profile, "proxy"))
        return ImportedIkevProfile(
            config = VpnProfileConfig(
                profileName = name,
                serverAddress = server,
                username = username,
            ),
            certificate = loadedCertificate,
            remoteId = remoteId,
            serverProfile = serverProfile,
            proxy = proxy,
        )
    }

    private fun parseProxy(proxy: JSONObject): ImportedProxyMetadata {
        val enabled = requiredBoolean(proxy, "enabled")
        if (!enabled) return ImportedProxyMetadata(enabled = false)

        val type = requiredString(proxy, "type")
        val host = requiredString(proxy, "host")
        val port = requiredInteger(proxy, "port")
        if (
            type != "socks5" ||
            !ProfileValidator.isValidIpv4Address(host) ||
            port !in 1L..65535L
        ) {
            throw IkevProfileImportException("The .ikev profile contains invalid proxy metadata.")
        }
        return ImportedProxyMetadata(
            enabled = true,
            type = type,
            host = host,
            port = port.toInt(),
        )
    }

    private fun requiredObject(container: JSONObject, name: String): JSONObject {
        return requiredValue(container, name) as? JSONObject
            ?: throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
    }

    private fun requiredString(container: JSONObject, name: String): String {
        val value = requiredValue(container, name) as? String
            ?: throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
        if (value.isBlank() || value.any(::isControlCharacter)) {
            throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
        }
        return value
    }

    private fun requiredBoolean(container: JSONObject, name: String): Boolean {
        return requiredValue(container, name) as? Boolean
            ?: throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
    }

    private fun requiredInteger(container: JSONObject, name: String): Long {
        return when (val value = requiredValue(container, name)) {
            is Int -> value.toLong()
            is Long -> value
            else -> throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
        }
    }

    private fun requiredValue(container: JSONObject, name: String): Any {
        if (!container.has(name)) throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE)
        return try {
            container.get(name)
        } catch (exception: JSONException) {
            throw IkevProfileImportException(UNSUPPORTED_PROFILE_MESSAGE, exception)
        }
    }

    private fun isControlCharacter(character: Char): Boolean {
        return Character.isISOControl(character)
    }

    private companion object {
        const val SUPPORTED_FORMAT_VERSION = 1
        const val UNSUPPORTED_PROFILE_MESSAGE = "This is not a supported IKEv profile."
        val USERNAME_PATTERN = Regex("^[A-Za-z0-9._@+\\-]{1,64}$")
        val SHA256_FINGERPRINT_PATTERN = Regex("^(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$")
        val SUPPORTED_SERVER_PROFILES = setOf("secure", "stock-windows-compatible")
    }
}

internal fun readBoundedIkevProfile(input: InputStream): ByteArray {
    val output = ByteArrayOutputStream()
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    var total = 0
    while (true) {
        val count = input.read(buffer)
        if (count < 0) break
        if (count == 0) {
            val singleByte = input.read()
            if (singleByte < 0) break
            total += 1
            if (total > MAX_IKEV_PROFILE_BYTES) {
                throw IkevProfileImportException("The .ikev profile is unexpectedly large.")
            }
            output.write(singleByte)
            continue
        }
        total += count
        if (total > MAX_IKEV_PROFILE_BYTES) {
            throw IkevProfileImportException("The .ikev profile is unexpectedly large.")
        }
        output.write(buffer, 0, count)
    }
    return output.toByteArray()
}

const val MAX_IKEV_PROFILE_BYTES = 1024 * 1024

class IkevProfileImportException(
    override val message: String,
    cause: Throwable? = null,
) : Exception(message, cause)
