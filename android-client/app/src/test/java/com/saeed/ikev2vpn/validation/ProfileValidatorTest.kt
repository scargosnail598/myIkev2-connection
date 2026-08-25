package com.saeed.ikev2vpn.validation

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ProfileValidatorTest {
    @Test
    fun `accepts valid hostname`() {
        assertTrue(ProfileValidator.isValidServerAddress("vpn.example.com"))
    }

    @Test
    fun `accepts valid IPv4 server`() {
        assertTrue(ProfileValidator.isValidServerAddress("203.0.113.10"))
    }

    @Test
    fun `rejects URL port and malformed IPv4`() {
        assertFalse(ProfileValidator.isValidServerAddress("https://vpn.example.com"))
        assertFalse(ProfileValidator.isValidServerAddress("vpn.example.com:500"))
        assertFalse(ProfileValidator.isValidServerAddress("999.0.0.1"))
    }

    @Test
    fun `rejects IPv6 literals outside the version one scope`() {
        assertFalse(ProfileValidator.isValidServerAddress("2001:db8::1"))
        assertFalse(ProfileValidator.isValidServerAddress("[2001:db8::1]"))
    }

    @Test
    fun `empty server is reported`() {
        val result = validInput(serverAddress = "")

        assertTrue(ProfileField.SERVER_ADDRESS in result.errors)
    }

    @Test
    fun `empty username is reported`() {
        val result = validInput(username = "")

        assertTrue(ProfileField.USERNAME in result.errors)
    }

    @Test
    fun `missing CA is reported`() {
        val result = validInput(hasCertificate = false)

        assertTrue(ProfileField.CERTIFICATE in result.errors)
    }

    @Test
    fun `empty profile name and password are reported`() {
        val result = ProfileValidator.validate(
            profileName = "",
            serverAddress = "vpn.example.com",
            username = "vpn-user",
            password = "",
            hasCertificate = true,
        )

        assertTrue(ProfileField.PROFILE_NAME in result.errors)
        assertTrue(ProfileField.PASSWORD in result.errors)
    }

    @Test
    fun `profile name allows portable names up to 128 characters and rejects controls`() {
        assertTrue(ProfileValidator.isValidProfileName("a".repeat(128)))
        assertFalse(ProfileValidator.isValidProfileName("a".repeat(129)))
        assertFalse(ProfileValidator.isValidProfileName("bad\nname"))
    }

    private fun validInput(
        serverAddress: String = "vpn.example.com",
        username: String = "vpn-user",
        hasCertificate: Boolean = true,
    ): ValidationResult = ProfileValidator.validate(
        profileName = "My VPN",
        serverAddress = serverAddress,
        username = username,
        password = "test-only-password",
        hasCertificate = hasCertificate,
    )
}
