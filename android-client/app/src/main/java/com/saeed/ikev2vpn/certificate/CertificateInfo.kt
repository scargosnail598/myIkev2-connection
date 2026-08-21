package com.saeed.ikev2vpn.certificate

import java.security.cert.X509Certificate

data class CertificateInfo(
    val displayName: String,
    val subject: String,
    val issuer: String,
    val sha256Fingerprint: String,
    val isCertificateAuthority: Boolean,
)

data class LoadedCertificate(
    val certificate: X509Certificate,
    val derBytes: ByteArray,
    val info: CertificateInfo,
)
