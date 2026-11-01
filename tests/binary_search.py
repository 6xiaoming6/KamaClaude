"""二分查找实现示例（迭代版 + 递归版）。"""

from __future__ import annotations

from collections.abc import Sequence


# 在有序序列中查找 target，返回其下标，找不到返回 -1
def binary_search(nums: Sequence[int], target: int) -> int:
    left, right = 0, len(nums) - 1
    while left <= right:
        mid = left + (right - left) // 2  # 避免 (left + right) 溢出
        if nums[mid] == target:
            return mid
        if nums[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1


# 递归版二分查找，区间语义为左闭右闭 [left, right]
def binary_search_recursive(
    nums: Sequence[int], target: int, left: int = 0, right: int | None = None
) -> int:
    if right is None:
        right = len(nums) - 1
    if left > right:
        return -1
    mid = left + (right - left) // 2
    if nums[mid] == target:
        return mid
    if nums[mid] < target:
        return binary_search_recursive(nums, target, mid + 1, right)
    return binary_search_recursive(nums, target, left, mid - 1)


# 查找 target 第一次出现的下标，不存在则返回 -1
def binary_search_leftmost(nums: Sequence[int], target: int) -> int:
    left, right = 0, len(nums) - 1
    found = -1
    while left <= right:
        mid = left + (right - left) // 2
        if nums[mid] == target:
            found = mid
            right = mid - 1  # 继续往左收缩，找更靠前的位置
        elif nums[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return found


# 命令行演示：在一组有序数字中查找用户给定的目标值
def _demo() -> None:
    data = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    print(f"有序数组: {data}")
    for target in (1, 9, 19, 8):
        idx = binary_search(data, target)
        print(f"binary_search({target}) -> {idx}")


if __name__ == "__main__":
    _demo()
