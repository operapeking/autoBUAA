<script setup lang="ts">
import { ref } from "vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { api, ApiError } from "../api/client";

const props = defineProps<{
  initialized: boolean;
}>();

const emit = defineEmits<{
  (e: "authenticated"): void;
}>();

const password = ref("");
const confirmPassword = ref("");
const loading = ref(false);
const error = ref("");

async function handleSubmit() {
  error.value = "";
  if (!password.value) {
    error.value = "请输入管理员密码";
    return;
  }
  if (!props.initialized) {
    if (password.value.length < 6) {
      error.value = "管理员密码至少 6 位";
      return;
    }
    if (password.value !== confirmPassword.value) {
      error.value = "两次输入的密码不一致";
      return;
    }
  }
  loading.value = true;
  try {
    if (props.initialized) {
      await api.auth.login(password.value);
    } else {
      await api.auth.setup(password.value);
    }
    emit("authenticated");
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : "认证失败";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="auth-page">
    <Card class="w-full max-w-sm p-7"><form @submit.prevent="handleSubmit">
      <h1 style="margin: 0 0 6px; font-size: 22px">autoBUAA</h1>
      <p style="margin: 0 0 18px; color: var(--text-muted)">
        {{ initialized ? "请输入管理员密码登录控制台" : "首次部署初始化：请设置单管理员密码" }}
      </p>

      <div v-if="error" class="error-box mb-4">{{ error }}</div>

      <div class="field" style="margin-bottom: 12px">
        <label for="admin-pwd">管理员密码</label>
        <Input
          id="admin-pwd"
          v-model="password"
          type="password"
          autocomplete="current-password"
          placeholder="请输入密码"
        />
      </div>

      <div v-if="!initialized" class="field" style="margin-bottom: 16px">
        <label for="admin-pwd-confirm">确认密码</label>
        <Input
          id="admin-pwd-confirm"
          v-model="confirmPassword"
          type="password"
          autocomplete="new-password"
          placeholder="再次输入密码"
        />
      </div>

      <Button style="width: 100%" type="submit" :disabled="loading">
        {{ loading ? "提交中..." : initialized ? "登录" : "完成初始化并登录" }}
      </Button>
    </form></Card>
  </div>
</template>
