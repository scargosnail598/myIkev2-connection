package com.saeed.ikev2vpn.certificate

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.util.Base64
import java.util.Date

class CertificateLoaderTest {
    private val loader = CertificateLoader {
        Date.from(Instant.parse("2030-01-01T00:00:00Z"))
    }

    @Test
    fun `loads valid DER CA certificate`() {
        val loaded = loader.load(derCertificate())

        assertEquals("IKEv2 Test Root CA", loaded.info.displayName)
        assertTrue(loaded.info.isCertificateAuthority)
        assertEquals(EXPECTED_FINGERPRINT, loaded.info.sha256Fingerprint)
    }

    @Test
    fun `loads valid PEM CA certificate`() {
        val fromPem = loader.load(PEM_CERTIFICATE.toByteArray())
        val fromDer = loader.load(derCertificate())

        assertArrayEquals(fromDer.derBytes, fromPem.derBytes)
        assertEquals(fromDer.info.sha256Fingerprint, fromPem.info.sha256Fingerprint)
    }

    @Test(expected = CertificateLoadException::class)
    fun `rejects invalid certificate`() {
        loader.load("not a certificate".toByteArray())
    }

    @Test(expected = CertificateLoadException::class)
    fun `rejects an empty certificate file`() {
        loader.load(byteArrayOf())
    }

    @Test(expected = CertificateLoadException::class)
    fun `rejects an oversized certificate file`() {
        loader.load(ByteArray(CertificateLoader.MAX_CERTIFICATE_BYTES + 1))
    }

    @Test(expected = CertificateLoadException::class)
    fun `rejects multiple certificates`() {
        loader.load("$PEM_CERTIFICATE\n$PEM_CERTIFICATE".toByteArray())
    }

    @Test
    fun `generates colon separated uppercase fingerprint`() {
        val fingerprint = CertificateLoader.sha256Fingerprint(derCertificate())

        assertEquals(EXPECTED_FINGERPRINT, fingerprint)
        assertEquals(32, fingerprint.split(':').size)
    }

    private fun derCertificate(): ByteArray = Base64.getDecoder().decode(DER_BASE64)

    internal companion object {
        const val EXPECTED_FINGERPRINT =
            "DE:08:FB:D4:54:FF:17:7B:70:2C:2A:56:B1:7F:09:91:" +
                "70:29:32:4B:46:75:DC:11:95:D6:49:71:21:88:B4:89"

        const val DER_BASE64 =
            "MIIDKzCCAhOgAwIBAgIUQoOKv3DQ5h9nk5neDBAjqZwE6EwwDQYJKoZIhvcNAQEL" +
                "BQAwHTEbMBkGA1UEAwwSSUtFdjIgVGVzdCBSb290IENBMB4XDTI2MDgyMTEyMTEy" +
                "OVoXDTM2MDgxODEyMTEyOVowHTEbMBkGA1UEAwwSSUtFdjIgVGVzdCBSb290IENB" +
                "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhkXX5nB/HqgzaKl5a3GV" +
                "AwLlJY6aSrZx/7qzFGCtAfyBPMJ5R+NUFReTHmZN4TvTefLtT7Jt8OrwJ+azqmDm" +
                "YQJbQzaJepKz9d42DniyVUDG4bX7yCVCkNeCZToUO9RaZCNRIbXiLPvIoralN//3" +
                "qUFYWevuvACgh34sLDIWOMtN5IxGeuz1/hviRe/U1TBdpL/xz4KQWU0KxiZvVyod" +
                "QrgayeNwt1JjoR9bIeoHw761WIxYdL+2ko17JDBa9ttBMhk3UYPDrbRBLdAwcfRS" +
                "bYx3bhw7T7iXcZTUlPVJajU8lFvjiopZ4A4W0yChLhdyorMZAR/fAYdjQXcLTbSN" +
                "ywIDAQABo2MwYTAdBgNVHQ4EFgQUQvxA0A8l7RVBR9+w/6PFKPH5WjAwHwYDVR0j" +
                "BBgwFoAUQvxA0A8l7RVBR9+w/6PFKPH5WjAwDwYDVR0TAQH/BAUwAwEB/zAOBgNV" +
                "HQ8BAf8EBAMCAQYwDQYJKoZIhvcNAQELBQADggEBACEagn7AAPuQLeU0QwyDXpOx" +
                "0b3yxtWQErUyAtTXx2l2Y9oqK74RgKKkYHN23T6QMjYZIULhMUe1fe2VBMqeOgT+" +
                "TNy4gLsTjJ9YhMrJ0vOUA7IDNh+IsD5Gdc5xv4FWJCKSqQIy86w0z0+OhEb9Nu5J" +
                "jvkoIs0+luclkYWg6U/iLawdVZdg/rArrOz4QTdi4USXPC7rzjoHpAsGkhmmcwbb" +
                "uDXVA0qllTmFFd1kTA/NqdKXLKllk3ya7hvykGuVuqsNdbm4QEonrtblB1pS/ko9" +
                "JIcWOT4KXEnBS207baeZf9bcP0Y4u9v74MVxWds4XEeP9YCa9pVZiETpK0+CxzA="

        val PEM_CERTIFICATE = """
            -----BEGIN CERTIFICATE-----
            MIIDKzCCAhOgAwIBAgIUQoOKv3DQ5h9nk5neDBAjqZwE6EwwDQYJKoZIhvcNAQEL
            BQAwHTEbMBkGA1UEAwwSSUtFdjIgVGVzdCBSb290IENBMB4XDTI2MDgyMTEyMTEy
            OVoXDTM2MDgxODEyMTEyOVowHTEbMBkGA1UEAwwSSUtFdjIgVGVzdCBSb290IENB
            MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAhkXX5nB/HqgzaKl5a3GV
            AwLlJY6aSrZx/7qzFGCtAfyBPMJ5R+NUFReTHmZN4TvTefLtT7Jt8OrwJ+azqmDm
            YQJbQzaJepKz9d42DniyVUDG4bX7yCVCkNeCZToUO9RaZCNRIbXiLPvIoralN//3
            qUFYWevuvACgh34sLDIWOMtN5IxGeuz1/hviRe/U1TBdpL/xz4KQWU0KxiZvVyod
            QrgayeNwt1JjoR9bIeoHw761WIxYdL+2ko17JDBa9ttBMhk3UYPDrbRBLdAwcfRS
            bYx3bhw7T7iXcZTUlPVJajU8lFvjiopZ4A4W0yChLhdyorMZAR/fAYdjQXcLTbSN
            ywIDAQABo2MwYTAdBgNVHQ4EFgQUQvxA0A8l7RVBR9+w/6PFKPH5WjAwHwYDVR0j
            BBgwFoAUQvxA0A8l7RVBR9+w/6PFKPH5WjAwDwYDVR0TAQH/BAUwAwEB/zAOBgNV
            HQ8BAf8EBAMCAQYwDQYJKoZIhvcNAQELBQADggEBACEagn7AAPuQLeU0QwyDXpOx
            0b3yxtWQErUyAtTXx2l2Y9oqK74RgKKkYHN23T6QMjYZIULhMUe1fe2VBMqeOgT+
            TNy4gLsTjJ9YhMrJ0vOUA7IDNh+IsD5Gdc5xv4FWJCKSqQIy86w0z0+OhEb9Nu5J
            jvkoIs0+luclkYWg6U/iLawdVZdg/rArrOz4QTdi4USXPC7rzjoHpAsGkhmmcwbb
            uDXVA0qllTmFFd1kTA/NqdKXLKllk3ya7hvykGuVuqsNdbm4QEonrtblB1pS/ko9
            JIcWOT4KXEnBS207baeZf9bcP0Y4u9v74MVxWds4XEeP9YCa9pVZiETpK0+CxzA=
            -----END CERTIFICATE-----
        """.trimIndent()
    }
}
