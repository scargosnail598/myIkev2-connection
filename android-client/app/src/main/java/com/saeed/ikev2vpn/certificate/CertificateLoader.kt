package com.saeed.ikev2vpn.certificate

import java.io.ByteArrayInputStream
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.CertificateExpiredException
import java.security.cert.CertificateFactory
import java.security.cert.CertificateNotYetValidException
import java.security.cert.X509Certificate
import java.util.Date

class CertificateLoader(
    private val validationDate: () -> Date = ::Date,
) {
    fun load(input: ByteArray): LoadedCertificate {
        if (input.isEmpty()) {
            throw CertificateLoadException("The certificate file is empty.")
        }
        if (input.size > MAX_CERTIFICATE_BYTES) {
            throw CertificateLoadException("The certificate file is too large.")
        }

        val certificate = try {
            val certificates = ByteArrayInputStream(input).use { stream ->
                CertificateFactory.getInstance("X.509").generateCertificates(stream)
            }
            if (certificates.size != 1) {
                throw CertificateLoadException("Select a file containing exactly one X.509 certificate.")
            }
            certificates.single() as? X509Certificate
                ?: throw CertificateLoadException("The selected file is not an X.509 certificate.")
        } catch (exception: CertificateLoadException) {
            throw exception
        } catch (exception: CertificateException) {
            throw CertificateLoadException("The certificate is not a valid X.509 certificate.", exception)
        }

        try {
            certificate.checkValidity(validationDate())
        } catch (exception: CertificateExpiredException) {
            throw CertificateLoadException("The CA certificate has expired.", exception)
        } catch (exception: CertificateNotYetValidException) {
            throw CertificateLoadException("The CA certificate is not valid yet.", exception)
        }

        val derBytes = try {
            certificate.encoded
        } catch (exception: CertificateException) {
            throw CertificateLoadException("The certificate could not be encoded.", exception)
        }
        val subject = certificate.subjectX500Principal.getName("RFC2253")
        val issuer = certificate.issuerX500Principal.getName("RFC2253")

        return LoadedCertificate(
            certificate = certificate,
            derBytes = derBytes,
            info = CertificateInfo(
                displayName = commonName(subject) ?: subject,
                subject = subject,
                issuer = issuer,
                sha256Fingerprint = sha256Fingerprint(derBytes),
                isCertificateAuthority = certificate.basicConstraints >= 0,
            ),
        )
    }

    private fun commonName(distinguishedName: String): String? {
        return distinguishedName
            .split(',')
            .firstOrNull { it.startsWith("CN=", ignoreCase = true) }
            ?.substringAfter('=')
            ?.replace("\\,", ",")
            ?.takeIf { it.isNotBlank() }
    }

    companion object {
        const val MAX_CERTIFICATE_BYTES = 256 * 1024

        fun sha256Fingerprint(derBytes: ByteArray): String {
            return MessageDigest.getInstance("SHA-256")
                .digest(derBytes)
                .joinToString(":") { byte -> "%02X".format(byte.toInt() and 0xFF) }
        }
    }
}

class CertificateLoadException(
    override val message: String,
    cause: Throwable? = null,
) : Exception(message, cause)
