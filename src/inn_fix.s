; SPDX-License-Identifier: MIT
; Copyright (c) 2026 Timo Heimonen <timo.heimonen@proton.me>
;
; Ultima V: Warriors of Destiny (Atari ST, 1988), AUTO\U5.PRG: fix for the
; inn's "Leave a companion" corrupting the active party member.
;
; Addresses are offsets from the start of the text segment, as the absolute
; addresses are stored in the executable before TOS relocates it.  The patch
; replaces code of the same size and contains no absolute addresses, so the
; relocation table does not change.
;
; Assemble (patch.py does this to verify its patch bytes):
;   vasmm68k_mot -quiet -no-opt -Fsrec -s28 -o inn_fix.srec inn_fix.s

active_member       equ $080d           ; byte in the saved-game block, $ff = no active member
active_done         equ $36dac          ; end of the active member update


; ---------------------------------------------------------------------------
; Inn, "Leave a companion": the member who stays is moved out of the party
; and the active member index is kept in step:
;
;   if (active == left)      active = $ff;
;   else if (active > left)  active--;
;
; The second test is unsigned, so the normal value $ff ("no active member")
; is always larger than the index and becomes $fe.  The shop code takes
; member #254 as the buyer; its record lies outside the party roster and its
; intelligence reads as 0, and prices are base * (200 - 3 * INT) / 100, so
; every price becomes the maximum (2 x base).
;
; Original code at $36d96:
;   moveq   #0,d0
;   move.b  (active_member).l,d0
;   cmp.l   (-10,a6),d0             ; index of the member who stays
;   bls.s   active_done
;   subq.l  #1,d0
;   move.b  d0,(active_member).l
;
; Fix: compare the low bytes as signed values.  $ff is -1 and never larger
; than an index; the indices 0..15 compare as before.
        org     $36d9e
        cmp.b   (-7,a6),d0              ; was: cmp.l (-10,a6),d0
        ble.s   active_done             ; was: bls.s active_done
