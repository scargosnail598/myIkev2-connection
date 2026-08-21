package com.saeed.ikev2vpn

import android.app.Application
import com.saeed.ikev2vpn.certificate.AndroidCertificateImporter
import com.saeed.ikev2vpn.certificate.CertificateImporter
import com.saeed.ikev2vpn.certificate.CertificateLoader
import com.saeed.ikev2vpn.data.DataStoreProfileRepository
import com.saeed.ikev2vpn.data.ProfileRepository
import com.saeed.ikev2vpn.vpn.AndroidVpnPlatformController

class Ikev2VpnApplication : Application() {
    lateinit var profileRepository: ProfileRepository
        private set
    lateinit var certificateImporter: CertificateImporter
        private set
    lateinit var vpnController: AndroidVpnPlatformController
        private set

    override fun onCreate() {
        super.onCreate()
        val certificateLoader = CertificateLoader()
        profileRepository = DataStoreProfileRepository(this, certificateLoader)
        certificateImporter = AndroidCertificateImporter(contentResolver, certificateLoader)
        vpnController = AndroidVpnPlatformController(this)
    }
}
