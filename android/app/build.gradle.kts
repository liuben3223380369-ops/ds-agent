plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.dsagent"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.dsagent"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        ndk {
            // arm64-v8a：真机；x86_64：模拟器。
            // armeabi-v7a / x86 已不支持 16KB 对齐（Android 15+ 要求），不再包含。
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    // 关键：把主项目的 Python 模块也纳入打包。
    // 第二个路径指向仓库根目录，这样 ds_agent_pro.py / terminal.py /
    // mcp_client.py 等都能被 import，不需要复制一份。
    sourceSets {
        getByName("main") {
            python.srcDirs("src/main/python", "../../")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

chaquopy {
    defaultConfig {
        // Android 15+ 强制 16KB 页对齐，必须用 3.13+；
        // 3.12 及以下的 .so 在 16KB 设备上会 dlopen 失败直接崩。
        version = "3.13"

        // 若 Gradle 自动找不到匹配的 Python，取消下一行注释并改成本机路径。
        // 注意：buildPython 的主次版本必须与上面的 version 一致（都要 3.13.x）。
        // buildPython("/usr/bin/python3.13")

        pip {
            // ds-agent 唯一的第三方依赖；其余全是标准库。
            install("openai")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.2.0")
    // Chaquopy 运行时由插件自动注入，无需手动添加
}
