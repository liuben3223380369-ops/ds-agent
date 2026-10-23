// 顶层：只声明插件版本，不 apply
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // Chaquopy 17.x 支持 AGP 7.3 ~ 9.2
    id("com.chaquo.python") version "17.1.0" apply false
}
