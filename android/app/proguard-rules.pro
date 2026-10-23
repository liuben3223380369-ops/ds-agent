# Chaquopy 依赖反射加载 Python 模块，不要混淆/裁剪
-keep class com.chaquo.python.** { *; }
-keep class com.dsagent.** { *; }

# Python 侧通过 Chaquopy 反射调用的方法
-keepclassmembers class * {
    public void onEvent(java.lang.String, java.lang.String);
}
