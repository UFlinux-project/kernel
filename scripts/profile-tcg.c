/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "qemu-plugin.h"

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

struct block {
    uint64_t first, last, next;
    bool branch;
};

struct edge {
    uint64_t from, to;
};

static GArray *blocks;
static GHashTable *ranges, *branches;
static uint64_t text_start, text_end, range_start;
static struct block previous;
static const char *output, *start_marker;
static bool collecting;
static bool arm64;
static unsigned polling;

static guint edge_hash(gconstpointer key)
{
    const struct edge *edge = key;
    return (guint)(edge->from * 31 + edge->to);
}

static gboolean edge_equal(gconstpointer a, gconstpointer b)
{
    const struct edge *left = a, *right = b;
    return left->from == right->from && left->to == right->to;
}

static void count(GHashTable *table, uint64_t from, uint64_t to)
{
    struct edge edge = {from, to};
    uint64_t *hits = g_hash_table_lookup(table, &edge);
    if (!hits) {
        struct edge *key = g_new(struct edge, 1);
        *key = edge;
        hits = g_new0(uint64_t, 1);
        g_hash_table_insert(table, key, hits);
    }
    ++*hits;
}

static void execute(unsigned int cpu, void *userdata)
{
    const struct block *block = &g_array_index(blocks, struct block,
                                               GPOINTER_TO_UINT(userdata));
    (void)cpu; /* The collector runs one vCPU. */
    if (!collecting) {
        if ((++polling & 4095) || access(start_marker, F_OK))
            return;
        collecting = true;
    }

    if (range_start && block->first != previous.next) {
        count(ranges, range_start, previous.last);
        if (previous.branch && block->first)
            count(branches, previous.last, block->first);
        range_start = 0;
    }
    if (!range_start)
        range_start = block->first;
    previous = *block;
}

static bool is_arm64_branch(const struct qemu_plugin_insn *insn)
{
    uint32_t instruction;
    if (qemu_plugin_insn_data(insn, &instruction, sizeof(instruction)) !=
        sizeof(instruction))
        return false;
    instruction = GUINT32_FROM_LE(instruction);
    return (instruction & 0x7c000000) == 0x14000000 || /* B, BL */
           (instruction & 0xff000000) == 0x54000000 || /* B.cond, BC.cond */
           (instruction & 0x7e000000) == 0x34000000 || /* CBZ, CBNZ */
           (instruction & 0x7e000000) == 0x36000000 || /* TBZ, TBNZ */
           (instruction & 0xfe000000) == 0xd6000000;   /* BR, BLR, RET */
}

static bool is_x86_branch(const struct qemu_plugin_insn *insn)
{
    uint8_t bytes[15];
    size_t length = qemu_plugin_insn_data(insn, bytes, sizeof(bytes));
    size_t i = 0;

    while (i < length) {
        uint8_t op = bytes[i++];
        /* Skip REX and legacy prefixes before decoding the opcode. */
        if ((op >= 0x40 && op <= 0x4f) || op == 0x66 || op == 0x67 ||
            op == 0xf2 || op == 0xf3 || op == 0x2e || op == 0x3e ||
            op == 0x26 || op == 0x36 || op == 0x64 || op == 0x65)
            continue;
        if (op == 0x0f)
            return i < length && bytes[i] >= 0x80 && bytes[i] <= 0x8f;
        if (op == 0xff)
            return i < length && (bytes[i] & 0x38) >= 0x10 &&
                   (bytes[i] & 0x38) <= 0x28;
        return (op >= 0x70 && op <= 0x7f) || (op >= 0xe0 && op <= 0xe3) ||
               op == 0xe8 || op == 0xe9 || op == 0xeb ||
               op == 0xc2 || op == 0xc3 || op == 0xca || op == 0xcb;
    }
    return false;
}

static void translate(qemu_plugin_id_t id, struct qemu_plugin_tb *tb)
{
    uint64_t first = qemu_plugin_tb_vaddr(tb);
    size_t instructions = qemu_plugin_tb_n_insns(tb);
    struct block block = {0};
    unsigned index = 0;

    (void)id;
    if (instructions && first >= text_start && first < text_end) {
        struct qemu_plugin_insn *last =
            qemu_plugin_tb_get_insn(tb, instructions - 1);
        block.first = first;
        block.last = qemu_plugin_insn_vaddr(last);
        block.next = block.last + qemu_plugin_insn_size(last);
        /* Treat an unexpected edge after a non-branch as an interrupt. */
        block.branch = arm64 ? is_arm64_branch(last) : is_x86_branch(last);
        index = blocks->len;
        g_array_append_val(blocks, block);
    }
    qemu_plugin_register_vcpu_tb_exec_cb(tb, execute, QEMU_PLUGIN_CB_NO_REGS,
                                        GUINT_TO_POINTER(index));
}

static void finish(qemu_plugin_id_t id, void *userdata)
{
    FILE *file = fopen(output, "w");
    GHashTable *tables[] = {ranges, branches};

    (void)id;
    (void)userdata;
    if (!file) {
        perror(output);
        return;
    }
    for (unsigned i = 0; i < G_N_ELEMENTS(tables); ++i) {
        GHashTableIter iter;
        gpointer key, value;
        g_hash_table_iter_init(&iter, tables[i]);
        while (g_hash_table_iter_next(&iter, &key, &value)) {
            const struct edge *edge = key;
            fprintf(file, "%c %" PRIx64 " %" PRIx64 " %" PRIu64 "\n",
                    i == 0 ? 'F' : 'B', edge->from, edge->to, *(uint64_t *)value);
        }
    }
    if (fclose(file))
        perror(output);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                          const qemu_info_t *info,
                                          int argc, char **argv)
{
    struct block outside_text = {0};

    arm64 = strcmp(info->target_name, "aarch64") == 0;
    if (!info->system_emulation ||
        (!arm64 && strcmp(info->target_name, "x86_64")) || argc != 4)
        return -1;
    for (int i = 0; i < argc; ++i) {
        const char *arg = argv[i];
        if (g_str_has_prefix(arg, "start="))
            text_start = strtoull(arg + strlen("start="), NULL, 16);
        else if (g_str_has_prefix(arg, "end="))
            text_end = strtoull(arg + strlen("end="), NULL, 16);
        else if (g_str_has_prefix(arg, "output="))
            output = g_strdup(arg + strlen("output="));
        else if (g_str_has_prefix(arg, "marker="))
            start_marker = g_strdup(arg + strlen("marker="));
        else
            return -1;
    }
    if (!text_start || text_end <= text_start || !output || !start_marker)
        return -1;
    blocks = g_array_new(FALSE, FALSE, sizeof(struct block));
    g_array_append_val(blocks, outside_text);
    ranges = g_hash_table_new_full(edge_hash, edge_equal, g_free, g_free);
    branches = g_hash_table_new_full(edge_hash, edge_equal, g_free, g_free);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate);
    qemu_plugin_register_atexit_cb(id, finish, NULL);
    return 0;
}
