package com.dsagent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import com.chaquo.python.Python

/**
 * 前台服务：让 Agent 在切后台后不被系统回收。
 *
 * Android 8+ 对后台服务限制很严：普通 Service 在应用进入后台后
 * 几分钟内就会被回收，切个微信回来任务就没了。前台服务 + 常驻通知
 * 是官方唯一给"长时间运行且用户可见"的通道。
 */
class AgentService : Service() {

    private var thread: Thread? = null

    /** Python 侧通过这个对象把输出推回来 */
    inner class Sink {
        fun onEvent(text: String, kind: String) {
            broadcast(text, kind)
        }
    }

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_START) {
            val task = intent.getStringExtra(EXTRA_TASK) ?: return START_NOT_STICKY
            val key = intent.getStringExtra(EXTRA_KEY) ?: ""
            startForeground(NOTIFICATION_ID, buildNotification(task))
            runTask(task, key)
        }
        return START_NOT_STICKY
    }

    private fun runTask(task: String, key: String) {
        thread = Thread {
            try {
                val py = Python.getInstance()
                val bridge = py.getModule("agent_bridge")
                bridge.callAttr("run", task, filesDir.absolutePath, key, Sink())
                broadcast("（任务结束）", "end")
            } catch (e: Exception) {
                Log.e(TAG, "任务失败", e)
                broadcast("任务异常：${e.javaClass.simpleName}: ${e.message}", "err")
                broadcast("（任务结束）", "end")
            } finally {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }.apply { isDaemon = true; start() }
    }

    private fun broadcast(text: String, kind: String) {
        val i = Intent(ACTION_EVENT).apply {
            setPackage(packageName)               // 显式广播，不外泄
            putExtra(EXTRA_TEXT, text)
            putExtra(EXTRA_KIND, kind)
        }
        sendBroadcast(i)
    }

    private fun buildNotification(task: String): Notification {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("ds-agent 运行中")
            .setContentText(task.take(60))
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            val ch = NotificationChannel(
                CHANNEL_ID, "Agent 运行状态",
                NotificationManager.IMPORTANCE_LOW
            ).apply { description = "显示 ds-agent 任务是否在运行" }
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        thread?.interrupt()
        super.onDestroy()
    }

    companion object {
        const val TAG = "ds-agent"
        const val CHANNEL_ID = "agent_run"
        const val NOTIFICATION_ID = 1
        const val ACTION_START = "com.dsagent.START"
        const val ACTION_EVENT = "com.dsagent.EVENT"
        const val EXTRA_TASK = "task"
        const val EXTRA_KEY = "api_key"
        const val EXTRA_TEXT = "text"
        const val EXTRA_KIND = "kind"
    }
}
