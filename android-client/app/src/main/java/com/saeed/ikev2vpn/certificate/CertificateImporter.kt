package com.saeed.ikev2vpn.certificate

import android.content.ContentResolver
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.IOException

interface CertificateImporter {
    suspend fun import(uri: Uri): LoadedCertificate
}

class AndroidCertificateImporter(
    private val contentResolver: ContentResolver,
    private val certificateLoader: CertificateLoader,
) : CertificateImporter {
    override suspend fun import(uri: Uri): LoadedCertificate = withContext(Dispatchers.IO) {
        val bytes = try {
            contentResolver.openInputStream(uri)?.use { input ->
                val output = ByteArrayOutputStream()
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var total = 0
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    total += count
                    if (total > CertificateLoader.MAX_CERTIFICATE_BYTES) {
                        throw CertificateLoadException("The certificate file is too large.")
                    }
                    output.write(buffer, 0, count)
                }
                output.toByteArray()
            } ?: throw CertificateLoadException("The CA certificate could not be read.")
        } catch (exception: CertificateLoadException) {
            throw exception
        } catch (exception: IOException) {
            throw CertificateLoadException("The CA certificate could not be read.", exception)
        } catch (exception: SecurityException) {
            throw CertificateLoadException("Access to the CA certificate was denied.", exception)
        }

        certificateLoader.load(bytes)
    }
}
