package com.saeed.ikev2vpn.profile

import com.saeed.ikev2vpn.certificate.CertificateLoader
import com.saeed.ikev2vpn.certificate.CertificateLoaderTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.time.Instant
import java.util.Date

class IkevProfileImporterTest {
    private val parser = IkevProfileParser(
        CertificateLoader { Date.from(Instant.parse("2030-01-01T00:00:00Z")) },
    )

    @Test
    fun `imports frozen version one profile with embedded CA and proxy metadata`() {
        val imported = parser.parse(validProfile().toByteArray())

        assertEquals("saeed@155.117.13.45", imported.config.profileName)
        assertEquals("155.117.13.45", imported.config.serverAddress)
        assertEquals("saeed", imported.config.username)
        assertEquals("155.117.13.45", imported.remoteId)
        assertEquals("secure", imported.serverProfile)
        assertEquals(
            CertificateLoaderTest.EXPECTED_FINGERPRINT,
            imported.certificate.info.sha256Fingerprint,
        )
        assertTrue(imported.proxy.enabled)
        assertEquals("socks5", imported.proxy.type)
        assertEquals("10.254.254.1", imported.proxy.host)
        assertEquals(1080, imported.proxy.port)
    }

    @Test
    fun `accepts proxy disabled without endpoint fields`() {
        val imported = parser.parse(validProfile(proxy = """{"enabled":false}""").toByteArray())

        assertFalse(imported.proxy.enabled)
        assertEquals(null, imported.proxy.host)
        assertEquals(null, imported.proxy.port)
    }

    @Test
    fun `rejects corrupted Base64`() {
        expectFailure("The embedded CA certificate is invalid.") {
            parser.parse(validProfile(caData = "%%%not-base64%%%").toByteArray())
        }
    }

    @Test
    fun `rejects fingerprint mismatch`() {
        val differentFingerprint = List(32) { "00" }.joinToString(":")

        expectFailure("CA certificate fingerprint verification failed.") {
            parser.parse(validProfile(fingerprint = differentFingerprint).toByteArray())
        }
    }

    @Test
    fun `rejects a valid end entity certificate`() {
        val fingerprint = CertificateLoader.sha256Fingerprint(
            java.util.Base64.getDecoder().decode(NON_CA_DER_BASE64),
        )

        expectFailure("The embedded certificate is not a CA certificate.") {
            parser.parse(
                validProfile(caData = NON_CA_DER_BASE64, fingerprint = fingerprint).toByteArray(),
            )
        }
    }

    @Test
    fun `rejects wrong format and future version explicitly`() {
        expectFailure("This is not a supported IKEv profile.") {
            parser.parse(validProfile(format = "invalid").toByteArray())
        }
        expectFailure("This profile uses .ikev format version 2.") {
            parser.parse(validProfile(version = "2").toByteArray())
        }
    }

    @Test
    fun `rejects non-integer version and unsupported authentication or mode`() {
        expectFailure("This is not a supported IKEv profile.") {
            parser.parse(validProfile(version = "1.0").toByteArray())
        }
        expectFailure("unsupported authentication") {
            parser.parse(validProfile(authentication = "certificate").toByteArray())
        }
        expectFailure("unsupported connection mode") {
            parser.parse(validProfile(mode = "split-tunnel").toByteArray())
        }
    }

    @Test
    fun `rejects remote ID mismatch and malformed proxy`() {
        expectFailure("Remote ID different from the server address") {
            parser.parse(validProfile(remoteId = "other.example.com").toByteArray())
        }
        expectFailure("invalid proxy metadata") {
            parser.parse(
                validProfile(
                    proxy = """{"enabled":true,"type":"socks5","host":"example.com","port":1080}""",
                ).toByteArray(),
            )
        }
    }

    @Test
    fun `bounded reader rejects more than one MiB`() {
        expectFailure("The .ikev profile is unexpectedly large.") {
            readBoundedIkevProfile(ByteArrayInputStream(ByteArray(MAX_IKEV_PROFILE_BYTES + 1)))
        }
    }

    @Test
    fun `rejects trailing JSON and invalid imported identity fields`() {
        expectFailure("This is not a supported IKEv profile.") {
            parser.parse("${validProfile()} trailing-data".toByteArray())
        }
        expectFailure("This is not a supported IKEv profile.") {
            parser.parse(
                validProfile()
                    .replace("saeed@155.117.13.45", "bad\\u0001name")
                    .toByteArray(),
            )
        }
        expectFailure("username is invalid") {
            parser.parse(
                validProfile()
                    .replace("\"username\": \"saeed\"", "\"username\": \"bad user\"")
                    .toByteArray(),
            )
        }
    }

    private fun expectFailure(messagePart: String, block: () -> Unit) {
        val exception = try {
            block()
            throw AssertionError("Expected import to fail")
        } catch (exception: IkevProfileImportException) {
            exception
        }
        assertTrue(exception.message.orEmpty(), exception.message.orEmpty().contains(messagePart))
    }

    private fun validProfile(
        format: String = "ikev-profile",
        version: String = "1",
        remoteId: String = "155.117.13.45",
        authentication: String = "eap-mschapv2",
        mode: String = "full-tunnel",
        caData: String = CertificateLoaderTest.DER_BASE64,
        fingerprint: String = CertificateLoaderTest.EXPECTED_FINGERPRINT.lowercase(),
        proxy: String = """{"enabled":true,"type":"socks5","host":"10.254.254.1","port":1080}""",
    ): String = """
        {
          "format": "$format",
          "version": $version,
          "name": "saeed@155.117.13.45",
          "server": "155.117.13.45",
          "remote_id": "$remoteId",
          "username": "saeed",
          "authentication": "$authentication",
          "ca_certificate": {
            "encoding": "der-base64",
            "data": "$caData",
            "sha256": "$fingerprint"
          },
          "connection": {"mode": "$mode"},
          "server_profile": "secure",
          "proxy": $proxy
        }
    """.trimIndent()

    private companion object {
        const val NON_CA_DER_BASE64 =
            "MIIDLjCCAhagAwIBAgIUd/FhaG0mCB3/W3qqAnZg6OzKMuUwDQYJKoZIhvcNAQEL" +
                "BQAwIDEeMBwGA1UEAwwVSUtFdjIgVGVzdCBFbmQgRW50aXR5MB4XDTI2MDgyNTA5NTcxNloX" +
                "DTM2MDgyMjA5NTcxNlowIDEeMBwGA1UEAwwVSUtFdjIgVGVzdCBFbmQgRW50aXR5MIIBIjAN" +
                "BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAsCsjcENgcHGqd0ROkJqE/0XeImhu1REx4h5r" +
                "nEie/x11uawMHt7FzQZe2kZ8grbZUbgF13fFSlobVYrOc+ymjkQJSjqlus8cieOlBXRMoxJs" +
                "huaGcAWCNNVrQbb547bjwJBDzjsOHS1wNLYGc7AxIRYR8imTtzGaoYYHjlAuVOvj1ewvPVIV" +
                "qhbOP6bY+IyzawBcBi0Jrk1l7U1qvX5Aor6nZvLbLayS83eV2X7i33N63CVahIRBXz+tlx3B" +
                "s9iuVbfqAHNmfJ5+wKLBNGHKkuO1Ic08SAHR29xtN0X8tKYmxjQsrvXtgtFZ9BcIW8SKqCQc" +
                "583vc3j3r8LBJwM1TQIDAQABo2AwXjAdBgNVHQ4EFgQUreOV97r7EHEQbqh4E+z5kEe9oHsw" +
                "HwYDVR0jBBgwFoAUreOV97r7EHEQbqh4E+z5kEe9oHswDAYDVR0TAQH/BAIwADAOBgNVHQ8B" +
                "Af8EBAMCBaAwDQYJKoZIhvcNAQELBQADggEBAFSdXEVAZ3WNrJuBWwuxwUbomEUqgLhs8OEM" +
                "WAi03t87kLMQ85oalnyQmzJOy21CDy0PphnLXlOMmfgf6FS2FlBou0U6af6jF+1LxxgvCljc" +
                "OcC/raVW5glOq8tosxnxvlm1IzTr6w6zmQXA1f+ljlVXqPJAtjPpwUDU8Kp0RMwjiBPijW1d" +
                "VXDMVkxuWIRN126LpbWmxHZ1gPCv7Zk36/1acTsDonRuHIcnD1VVipUju/rHz1l1Z5yr4ISO" +
                "CgaZUq+hiW53d2dRDEwhceNeeWYQq1qlKkIxyY78VvGXEPNo4iBI87UxTHCv+n1qcUR/P2pb" +
                "LwNo1hKsw0tEF24I7ZM="
    }
}
