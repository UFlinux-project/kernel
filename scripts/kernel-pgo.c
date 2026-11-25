// SPDX-License-Identifier: GPL-2.0
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/proc_fs.h>

extern char __uflinux_pgo_counters[], __uflinux_pgo_counters_end[];

static ssize_t read_pgo_counters(struct file *file, char __user *buffer,
				 size_t size, loff_t *offset)
{
	return simple_read_from_buffer(buffer, size, offset, __uflinux_pgo_counters,
				       __uflinux_pgo_counters_end - __uflinux_pgo_counters);
}

static const struct proc_ops pgo_ops = {
	.proc_read = read_pgo_counters,
	.proc_lseek = default_llseek,
};

static int __init expose_pgo_counters(void)
{
	return proc_create("uflinux-pgo-counters", 0400, NULL, &pgo_ops) ? 0 : -ENOMEM;
}
late_initcall(expose_pgo_counters);
