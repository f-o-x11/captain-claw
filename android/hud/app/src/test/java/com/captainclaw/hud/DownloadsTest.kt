package com.captainclaw.hud

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DownloadsTest {
    @Test
    fun fileNameFromContentDisposition() {
        assertEquals("report.pdf", Downloads.fileName("attachment; filename=\"report.pdf\""))
        assertEquals("plain.txt", Downloads.fileName("inline; filename=plain.txt"))
        // RFC 5987 wins over the ASCII fallback (Starlette sends both for non-ASCII names).
        assertEquals("Čevapi menu.pdf", Downloads.fileName("attachment; filename*=utf-8''%C4%8Cevapi%20menu.pdf; filename=\"_evapi menu.pdf\""))
        assertEquals("a \"quoted\" name.txt", Downloads.fileName("attachment; filename=\"a \\\"quoted\\\" name.txt\""))
        assertNull(Downloads.fileName("inline"))
        assertNull(Downloads.fileName(null))
    }

    @Test
    fun safeNames() {
        assertEquals("passwd", Downloads.safeName("../../etc/passwd", null))
        assertEquals("x.txt", Downloads.safeName("C:\\temp\\x.txt", null))
        assertEquals("a_b.csv", Downloads.safeName("a:b.csv", null))
        assertEquals("download", Downloads.safeName("", null))
        assertEquals("env", Downloads.safeName(".env", null))
    }
}
