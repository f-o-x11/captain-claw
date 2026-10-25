// Captain Claw for Android — a thin WebView shell around a Flight Deck's kiosk.
// AGP 9 compiles Kotlin itself (no separate Kotlin plugin).

plugins {
    alias(libs.plugins.android.application)
}

android {
    namespace = "com.captainclaw.hud"
    compileSdk {
        version = release(36) { minorApiLevel = 1 }
    }

    defaultConfig {
        applicationId = "com.captainclaw.hud"
        minSdk = 26
        targetSdk = 36
        versionCode = 3
        versionName = "0.3.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(libs.androidx.activity.ktx)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.webkit)
    testImplementation(libs.junit)
}
