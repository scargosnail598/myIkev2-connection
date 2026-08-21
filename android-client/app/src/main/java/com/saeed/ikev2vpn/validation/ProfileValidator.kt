package com.saeed.ikev2vpn.validation

import java.net.IDN

enum class ProfileField {
    PROFILE_NAME,
    SERVER_ADDRESS,
    USERNAME,
    PASSWORD,
    CERTIFICATE,
}

data class ValidationResult(
    val errors: Map<ProfileField, String>,
) {
    val isValid: Boolean get() = errors.isEmpty()
}

object ProfileValidator {
    fun validate(
        profileName: String,
        serverAddress: String,
        username: String,
        password: String,
        hasCertificate: Boolean,
    ): ValidationResult {
        val errors = buildMap {
            if (profileName.isBlank()) {
                put(ProfileField.PROFILE_NAME, "Enter a profile name.")
            } else if (profileName.trim().length > 64) {
                put(ProfileField.PROFILE_NAME, "Profile name must be 64 characters or fewer.")
            }

            when {
                serverAddress.isBlank() -> put(ProfileField.SERVER_ADDRESS, "Enter the VPN server address.")
                !isValidServerAddress(serverAddress.trim()) -> put(
                    ProfileField.SERVER_ADDRESS,
                    "Enter a valid hostname or IPv4 address without a scheme, path, or port.",
                )
            }

            if (username.isBlank()) {
                put(ProfileField.USERNAME, "Enter the VPN username.")
            }
            if (password.isEmpty()) {
                put(ProfileField.PASSWORD, "Enter the VPN password.")
            }
            if (!hasCertificate) {
                put(ProfileField.CERTIFICATE, "Import the VPN CA certificate.")
            }
        }
        return ValidationResult(errors)
    }

    fun isValidServerAddress(value: String): Boolean {
        if (value.isBlank() || value != value.trim()) return false
        if (value.contains("://") || value.contains('/') || value.contains(':')) return false
        if (value.matches(Regex("[0-9.]+"))) return isValidIpv4(value)

        val ascii = try {
            IDN.toASCII(value, IDN.USE_STD3_ASCII_RULES)
        } catch (_: IllegalArgumentException) {
            return false
        }
        if (ascii.length !in 1..253) return false
        val normalized = ascii.removeSuffix(".")
        if (normalized.isBlank()) return false
        return normalized.split('.').all { label ->
            label.length in 1..63 && HOST_LABEL.matches(label)
        }
    }

    private fun isValidIpv4(value: String): Boolean {
        val octets = value.split('.')
        return octets.size == 4 && octets.all { octet ->
            octet.isNotEmpty() &&
                octet.length <= 3 &&
                octet.all(Char::isDigit) &&
                octet.toIntOrNull() in 0..255
        }
    }

    private val HOST_LABEL = Regex("[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
}
