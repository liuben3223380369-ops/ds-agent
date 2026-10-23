package com.dsagent

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * 在 Application.onCreate 里启动 Python。
 *
 * 必须在这里做而不是 Activity：Python.start() 每进程只能调一次，
 * 而 Activity 可能因旋转/回收被重建多次。
 */
class AgentApp : Application() {

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            try {
                Python.start(AndroidPlatform(this))
                Log.i(TAG, "Python 已启动")
            } catch (e: Exception) {
                // 启动失败不能静默：后面所有 Python 调用都会抛
                Log.e(TAG, "Python 启动失败", e)
            }
        }
    }

    companion object {
        const val TAG = "ds-agent"
    }
}
